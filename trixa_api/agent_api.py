"""Agent-API (``/agent/*``) — per-adept-scope:ad yta för extern AI (Nils m.fl.).

Varje endpoint härleder adepten ur Bearer-token (se ``agent_auth``). Det finns
ingen ``athlete_user_id``-parameter — en token kan bara röra sin egen adept.
Detta är den provider-agnostiska kontraktsytan: vilken AI som helst som kan
göra HTTP + Bearer kan koppla upp sig, utan rå DB-åtkomst.

Skiljs medvetet från det interna ``/api/*`` (delad ``TRIXA_API_TOKEN``), som är
admin/dev-ytan med bredare åtkomst.
"""

from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from coach.trixa.db import get_postgrest
from trixa_api.agent_auth import AgentScope, resolve_agent_scope

router = APIRouter(prefix="/agent", tags=["agent"])

# Discipliner: lagras svenska i planned_sessions, exponeras engelska i läs-svar.
_EN_TO_SV = {
    "bike": "Cykel", "run": "Löpning", "swim": "Sim",
    "strength": "Styrka", "rest": "Vila", "brick": "Brick",
}
_SV_TO_EN = {
    "Cykel": "bike", "Cykling": "bike", "Löpning": "run", "Lopning": "run",
    "Sim": "swim", "Simning": "swim", "Styrka": "strength", "Vila": "rest",
    "Brick": "brick", "Yoga": "rest", "Promenad": "rest",
}


def _norm_sport_sv(sport: str) -> str:
    """Engelska ELLER svenska in → svenskt lagringsnamn (planned_sessions)."""
    s = (sport or "").strip()
    if s in _EN_TO_SV:
        return _EN_TO_SV[s]
    if s in _SV_TO_EN:  # redan svenska
        return s
    return s.capitalize() or "Vila"


def _monday_of(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


# ---------- sanity ----------


@router.get("/whoami")
def whoami(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """Verifiera token: vilken adept är jag scope:ad till?"""
    client = get_postgrest()
    name = None
    try:
        p = client.table("profiles").select("name").eq("id", scope.user_id).limit(1).execute()
        name = p.data[0].get("name") if p.data else None
    except Exception:  # noqa: BLE001
        pass
    return {"user_id": scope.user_id, "athlete_name": name, "token_name": scope.name}


# ---------- läsa: athlete-state ----------


@router.get("/athlete")
def get_athlete(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """Adeptens mål, testvärden, hälsa — Nils kontextbygge."""
    client = get_postgrest()
    res = (
        client.table("athlete_profiles")
        .select("id, user_id, goal, experience_level, weekly_hours, weekly_days,"
                " race_type, race_date, time_goal, ftp, lthr, swim_css,"
                " run_threshold_pace, injuries, health_conditions, active_concerns,"
                " medications, phase_state, notes")
        .eq("user_id", scope.user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Athlete saknas")
    return res.data[0]


# ---------- läsa: veckans plan ----------


def _week_plan(client, user_id: str, monday: date_type) -> dict:
    sunday = monday + timedelta(days=6)
    res = (
        client.table("planned_sessions")
        .select("id, date, sport, title, workout_code, intensity, duration_min,"
                " details, purpose, status, origin")
        .eq("user_id", user_id)
        .gte("date", monday.isoformat())
        .lte("date", sunday.isoformat())
        .order("date")
        .execute()
    )
    sessions = [
        {
            "id": w["id"],
            "date": w["date"],
            "sport": _SV_TO_EN.get(w.get("sport"), (w.get("sport") or "").lower()),
            "title": w.get("title") or "",
            "workout_code": w.get("workout_code") or "",
            "intensity": w.get("intensity") or "",
            "duration_min": w.get("duration_min"),
            "details": w.get("details") or "",
            "status": w.get("status") or "",
            "origin": w.get("origin") or "",
        }
        for w in (res.data or [])
    ]
    return {"week_start": monday.isoformat(), "sessions": sessions}


@router.get("/week/current")
def get_current_week(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """Veckan som innehåller dagens datum (ur MASTER planned_sessions)."""
    return _week_plan(get_postgrest(), scope.user_id, _monday_of(date_type.today()))


@router.get("/week")
def get_week(
    monday: date_type = Query(..., description="Veckans måndag (YYYY-MM-DD)"),
    scope: AgentScope = Depends(resolve_agent_scope),
) -> dict:
    """Plan för en godtycklig vecka (ange måndagen)."""
    return _week_plan(get_postgrest(), scope.user_id, _monday_of(monday))


# ---------- läsa: utfört ----------


@router.get("/log")
def get_log(
    since: date_type | None = Query(None, description="Från och med datum (default 28 d bakåt)"),
    limit: int = Query(60, ge=1, le=365),
    scope: AgentScope = Depends(resolve_agent_scope),
) -> dict:
    """Genomförda pass ur MASTER training_log (alla källor)."""
    client = get_postgrest()
    start = (since or (date_type.today() - timedelta(days=28))).isoformat()
    res = (
        client.table("training_log")
        .select("date, sport, title, duration_min, distance_km, avg_hr, max_hr,"
                " avg_power, normalized_power, tss, source")
        .eq("user_id", scope.user_id)
        .gte("date", start)
        .order("date", desc=True)
        .limit(limit)
        .execute()
    )
    return {"since": start, "sessions": res.data or []}


# ---------- läsa: recovery ----------


@router.get("/recovery")
def get_recovery(
    days: int = Query(14, ge=1, le=90),
    scope: AgentScope = Depends(resolve_agent_scope),
) -> dict:
    """HRV/sömn/RHR/load ur garmin_coach.daily_metrics (via garmin_athlete_id)."""
    client = get_postgrest()
    a = (
        client.table("athlete_profiles").select("garmin_athlete_id")
        .eq("user_id", scope.user_id).limit(1).execute()
    )
    gid = a.data[0].get("garmin_athlete_id") if a.data else None
    if not gid:
        return {"metrics": [], "note": "Ingen garmin_athlete_id kopplad."}
    res = (
        client.schema("garmin_coach").table("daily_metrics")
        .select("metric_date, resting_hr, hrv_last_night_ms, hrv_baseline_low,"
                " hrv_baseline_high, sleep_score, readiness_score, stress_avg, load_ratio")
        .eq("athlete_id", gid)
        .order("metric_date", desc=True)
        .limit(days)
        .execute()
    )
    return {"metrics": res.data or []}


# ---------- skriva: plan (Nils vinner) ----------


class PlanSessionIn(BaseModel):
    date: date_type
    sport: str = Field(..., description="bike/run/swim/strength/rest (eller svenska)")
    title: str
    duration_min: int | None = None
    intensity: str = ""
    details: str = ""
    workout_code: str = ""


@router.post("/plan/session")
def write_plan_session(
    body: PlanSessionIn, scope: AgentScope = Depends(resolve_agent_scope)
) -> dict:
    """Skriv ett pass i planen (origin='nils'). Upsert på (adept, datum, gren).

    Databasen har UNIQUE (user_id, date, sport) — två pass för samma gren samma
    dag kan alltså inte existera. Därför matchar vi på hela nyckeln, **oavsett
    origin**: ligger motorns eller adeptens egen rad där tar coachen över den.
    Att bara leta efter egna rader (origin='nils') gjorde att varje krock med en
    genererad vecka slog i unik-indexet och kastade ett rått databasfel, trots
    att endpointen dokumenterats som upsert.

    Att ta över raden är också rätt semantik: plannerns grind hoppar över dagar
    som redan har mänskligt skapade rader, så en övertagen rad blir skyddad.
    """
    client = get_postgrest()
    sport_sv = _norm_sport_sv(body.sport)
    row = {
        "user_id": scope.user_id,
        "date": body.date.isoformat(),
        "sport": sport_sv,
        "title": body.title.strip() or "Pass",
        "duration_min": body.duration_min,
        "intensity": body.intensity.strip(),
        "details": body.details.strip(),
        "workout_code": body.workout_code.strip(),
        "status": "planned",
        "origin": "nils",
    }

    def _existing() -> dict | None:
        res = (
            client.table("planned_sessions").select("id, origin")
            .eq("user_id", scope.user_id).eq("date", row["date"]).eq("sport", sport_sv)
            .limit(1).execute()
        )
        return (res.data or [None])[0]

    def _took_over(found: dict) -> dict:
        # Läs av vad som stod där INNAN uppdateringen — annars rapporterar vi
        # tillbaka vårt eget origin och adepten får aldrig veta att motorns
        # eller hens eget pass skrevs över.
        previous = found.get("origin")
        session_id = found["id"]
        client.table("planned_sessions").update(row).eq("id", session_id).execute()
        return {
            "status": "ok", "id": session_id, "sport": sport_sv, "origin": "nils",
            "replaced_origin": previous,
        }

    found = _existing()
    if found is not None:
        return _took_over(found)

    try:
        res = client.table("planned_sessions").insert(row).execute()
    except Exception:  # noqa: BLE001
        # Kapplöpning: någon hann skriva raden mellan uppslaget och insert:en.
        # Unik-indexet gjorde sitt jobb — läs om och uppdatera i stället.
        found = _existing()
        if found is None:
            raise
        return _took_over(found)
    sid = res.data[0]["id"] if res.data else None
    return {"status": "ok", "id": sid, "sport": sport_sv, "origin": "nils",
            "replaced_origin": None}


@router.delete("/plan/session/{session_id}")
def delete_plan_session(
    session_id: str, scope: AgentScope = Depends(resolve_agent_scope)
) -> dict:
    """Ta bort ett pass — bara adeptens egna rader (scope skyddar)."""
    client = get_postgrest()
    owner = (
        client.table("planned_sessions").select("id, origin")
        .eq("id", session_id).eq("user_id", scope.user_id).limit(1).execute()
    )
    if not owner.data:
        raise HTTPException(404, "Pass saknas eller tillhör inte denna adept.")
    client.table("planned_sessions").delete().eq("id", session_id).eq(
        "user_id", scope.user_id
    ).execute()
    return {"status": "deleted", "id": session_id}


# ---------- skriva: override ----------


class OverrideIn(BaseModel):
    scope: str = Field(..., description="week|workout|phase|volume|overtraining")
    engine_recommendation: dict
    override_decision: dict
    motivation: str = Field(..., min_length=10)
    medical_context_disclosed: bool = False
    athlete_explicit_request: bool = False
    # Namnen speglar kolumnerna i coach_overrides. Hette week_id/workout_id
    # förut, kolumner som aldrig funnits, så varje skrivning mot skarp DB gav
    # PGRST204. CHECK-constraintet scope_matches_target kräver dessutom
    # week_start när scope=week och planned_session_id när scope=workout.
    week_start: date_type | None = None
    planned_session_id: str | None = None


@router.post("/override")
def write_override(
    body: OverrideIn, scope: AgentScope = Depends(resolve_agent_scope)
) -> dict:
    """Skapa en manual_override (coachen åsidosätter engine).

    athlete_id = athlete_profiles.id. coach_user_id slås upp via coach_athletes,
    men **faller tillbaka på adepten själv** när ingen mänsklig coach är kopplad.
    Tidigare gav det 404, vilket gjorde verktyget oanvändbart för varje
    självcoachad adept, trots att spårbarheten behövs mest just då: det är en
    språkmodell som avviker från motorn, och beslutet måste gå att granska
    efteråt.
    """
    client = get_postgrest()
    a = (
        client.table("athlete_profiles").select("id")
        .eq("user_id", scope.user_id).limit(1).execute()
    )
    if not a.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a.data[0]["id"]
    coach = (
        client.table("coach_athletes").select("coach_id")
        .eq("athlete_id", scope.user_id).in_("status", ["accepted", "active"])
        .limit(1).execute()
    )
    coach_user_id = coach.data[0]["coach_id"] if coach.data else scope.user_id
    self_coached = not coach.data

    # scope_matches_target i DB: week kräver week_start, workout kräver
    # planned_session_id. Fånga det här i stället för som ett rått CHECK-fel.
    if body.scope == "week" and not body.week_start:
        raise HTTPException(400, "scope=week kräver week_start (veckans måndag).")
    if body.scope == "workout" and not body.planned_session_id:
        raise HTTPException(400, "scope=workout kräver planned_session_id.")

    row = {
        "athlete_id": athlete_id,
        "coach_user_id": coach_user_id,
        "scope": body.scope,
        "week_start": body.week_start.isoformat() if body.week_start else None,
        "planned_session_id": body.planned_session_id,
        "engine_recommendation": body.engine_recommendation,
        "override_decision": body.override_decision,
        "motivation": body.motivation,
        "medical_context_disclosed": body.medical_context_disclosed,
        "athlete_explicit_request": body.athlete_explicit_request,
    }
    res = client.table("coach_overrides").insert(row).execute()
    if not res.data:
        raise HTTPException(500, "Override-insert gav ingen data.")
    return {"status": "ok", "id": res.data[0]["id"], "self_coached": self_coached}
