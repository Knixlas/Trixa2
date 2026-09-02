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

from functools import lru_cache

from coach.trixa import clock, sports
from coach.trixa.db import get_postgrest
from coach.trixa.exercise_plan import normalize_exercises, planned_exercises
from coach.trixa.strength_progression import apply_suggestions
from coach.trixa.training_log import dedup_cross_source
from trixa_api.agent_auth import AgentScope, resolve_agent_scope


@lru_cache(maxsize=1)
def _exercise_catalogue() -> dict[str, dict]:
    """Passbankens övningskatalog, kod → post. Läses en gång per process."""
    from coach.engine.loader import load_strength_exercises

    return {e["code"]: e for e in load_strength_exercises()}

router = APIRouter(prefix="/agent", tags=["agent"])

# Så långt bakåt styrkeprogressionen läser loggen (se ui._PROGRESSION_DAYS).
_PROGRESSION_DAYS = 120

# Discipliner: lagras svenska i planned_sessions, exponeras engelska i läs-svar.
class _SvToEnMap(dict):
    """Svensk etikett → disciplin via registret. Behåller dict-formen för
    de läsare som gör .get(sport, fallback)."""

    def get(self, key, default=None):  # noqa: D102
        return sports.canon(key, default)


_SV_TO_EN = _SvToEnMap()


def _norm_sport_sv(sport: str) -> str:
    """Engelska ELLER svenska in → kanoniskt svenskt lagringsnamn.

    Förut returnerades svenska alias verbatim ("Cykling", "Simning") och
    okända ord kapitaliserades ("biking" → "Biking"). Raderna gick inte att
    matcha mot utfört, och TP fick dem som "Other". Registret ger alltid
    "Cykel"; okänt avvisas i stället för att bli en gren ingen känner till.
    """
    key = sports.canon(sport)
    if key is None:
        raise HTTPException(
            400, f"Okänd gren: {sport!r}. Använd en av {', '.join(sports.PLANNABLE_KEYS)}."
        )
    return sports.sv(key)


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


# Hela adeptprofilen som ``/ui/settings`` och ``/ui/health`` håller. Listan var
# förut en delmängd: den utelämnade aktiva discipliner, vilodagar, utrustning och
# pool-tillgång — precis de fält som avgör vad som ÖVERHUVUDTAGET går att lägga.
# En agent som bara ser MCP-ytan planerade därför blint, och skrev sim- och
# cykelpass åt en adept som hade båda avstängda. Allt som styr planeringen ska
# vara läsbart här; det som inte gör det (tokens, cookies) hör inte hemma.
_ATHLETE_COLUMNS = (
    "id, user_id, coach_name, goal, experience_level, weekly_hours, weekly_days,"
    " race_type, race_date, time_goal,"
    " ftp, lthr, lthr_bike, max_hr, resting_hr, swim_css, run_threshold_pace,"
    " threshold_meta, recovery_week_ratio,"
    " sports, preferred_rest_days, long_bike_day, long_run_day,"
    " equipment, preferred_settings,"
    " injuries, health_conditions, active_concerns, medications,"
    " nutrition_notes, race_carbs_per_hour_g, carb_load_g_per_kg,"
    " phase_state, notes, onboarded_at, onboarding_version"
)

_ALL_SPORTS = ("swim", "bike", "run", "strength")
_IMPACT_RANK = {"none": 0, "partial": 1, "full": 2}
_DAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


def _athlete_row(client, user_id: str) -> dict:
    res = (
        client.table("athlete_profiles")
        .select(_ATHLETE_COLUMNS)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(404, "Athlete saknas")
    return res.data[0]


@router.get("/athlete")
def get_athlete(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """Hela adeptprofilen: mål, testvärden, schema, utrustning, hälsa."""
    return _athlete_row(get_postgrest(), scope.user_id)


def _worst_impact(concerns: list) -> dict[str, str]:
    """Värsta impact per disciplin över alla aktiva besvär.

    Två besvär kan träffa samma gren olika hårt — planeringen måste följa det
    strängaste. Okända värden räknas som ``none`` (fältet är fritext i jsonb).
    """
    worst = {s: "none" for s in _ALL_SPORTS}
    for concern in concerns or []:
        if not isinstance(concern, dict):
            continue
        impacts = concern.get("impact_per_discipline") or {}
        for sport, level in impacts.items():
            if sport not in worst:
                continue
            if _IMPACT_RANK.get(level, 0) > _IMPACT_RANK.get(worst[sport], 0):
                worst[sport] = level
    return worst


@router.get("/constraints")
def get_constraints(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """De hårda begränsningarna, färdigsammanvägda: vad går att planera alls?

    ``get_athlete`` bär råvärdena, men de ligger på tre ställen (aktiva
    discipliner, utrustning, impact per besvär) och måste vägas ihop rätt för
    att ge ett svar. Den sammanvägningen är deterministisk och hör hemma i
    koden, inte i en språkmodells huvud — därför den här vyn.
    """
    athlete = _athlete_row(get_postgrest(), scope.user_id)

    sports = [s for s in (athlete.get("sports") or list(_ALL_SPORTS)) if s in _ALL_SPORTS]
    equipment = athlete.get("equipment") or {}
    pool = equipment.get("pool_type") or "unknown"
    impact = _worst_impact(athlete.get("active_concerns") or [])

    blocked = sorted(s for s in sports if impact.get(s) == "full")
    limited = sorted(s for s in sports if impact.get(s) == "partial")
    # Ingen pool och inget öppet vatten → simpass går inte att genomföra,
    # oavsett vad profilen säger att adepten håller på med.
    if "swim" in sports and pool == "none" and "swim" not in blocked:
        blocked.append("swim")
    plannable = [s for s in sports if s not in blocked]

    rest_days = [d for d in (athlete.get("preferred_rest_days") or []) if d in _DAY_NAMES]

    reasons: list[str] = []
    inactive = [s for s in _ALL_SPORTS if s not in sports]
    if inactive:
        reasons.append(
            "Ej aktiva discipliner (planera inga pass alls i dem): "
            + ", ".join(inactive)
        )
    if pool == "none" and "swim" in sports:
        reasons.append("Ingen pool-tillgång — simpass går inte att genomföra.")
    for sport in blocked:
        if impact.get(sport) == "full":
            reasons.append(f"{sport}: besvär med impact 'full' — hoppa över helt.")
    for sport in limited:
        reasons.append(f"{sport}: besvär med impact 'partial' — bara lugna AE-pass.")
    if rest_days:
        reasons.append("Vilodagar (lägg inga pass): " + ", ".join(rest_days))

    return {
        "sports": sports,
        "inactive_sports": inactive,
        "plannable_sports": plannable,
        "blocked_sports": blocked,
        "limited_sports": limited,
        "discipline_impact": impact,
        "rest_days": rest_days,
        "long_session_days": {
            "bike": athlete.get("long_bike_day"),
            "run": athlete.get("long_run_day"),
        },
        "pool_access": pool,
        "equipment": equipment,
        "preferred_settings": athlete.get("preferred_settings") or {},
        "weekly_hours": athlete.get("weekly_hours"),
        "weekly_days": athlete.get("weekly_days"),
        "reasons": reasons,
    }


# ---------- läsa: veckans plan ----------


def _week_plan(client, user_id: str, monday: date_type) -> dict:
    sunday = monday + timedelta(days=6)
    res = (
        client.table("planned_sessions")
        .select("id, date, sport, title, workout_code, intensity, duration_min,"
                " details, purpose, status, origin, exercises, steps")
        .eq("user_id", user_id)
        .gte("date", monday.isoformat())
        .lte("date", sunday.isoformat())
        .order("date")
        .execute()
    )
    # 120 dagars exercise_logs hämtades även för veckor utan ett enda
    # styrkepass (docs/12 H6). Bara när det finns något att räkna på.
    rows = [w for w in (res.data or []) if w.get("status") != "cancelled"]
    has_strength = any(sports.canon(w.get("sport")) == "strength" for w in rows)
    history = _strength_history(client, user_id, monday, sunday) if has_strength else []
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
            # Övningarna bär nästa last räknad ur förra passets ansträngning,
            # samma tal som adeptens loggformulär visar. Coachen ska se det
            # adepten ser — annars föreslår hen vikter som motsäger appen.
            # Bara loggar FÖRE passets datum räknas: ett senare pass i veckan
            # får inte styra ett tidigare bakåt i tiden.
            "exercises": apply_suggestions(
                planned_exercises(w, _exercise_catalogue()),
                [h for h in history if str(h.get("session_date"))[:10] < str(w["date"])[:10]],
                coach_prescribed=(w.get("origin") or "") != "trixa2",
            ),
            "status": w.get("status") or "",
            "origin": w.get("origin") or "",
        }
        for w in rows   # cancelled bortfiltrerade ovan (docs/12 F4)
    ]
    return {"week_start": monday.isoformat(), "sessions": sessions}


def _strength_history(
    client, user_id: str, monday: date_type, sunday: date_type
) -> list[dict]:
    """Styrkeloggen fram till veckans slut — underlaget progressionen räknar på.

    Veckans egna loggar tas med så att coachens siffror inte släpar efter
    adeptens app mitt i veckan; anroparen skär bort allt som ligger efter det
    pass som räknas.
    """
    try:
        res = (
            client.table("exercise_logs")
            .select("session_date, exercise_name, exercise_code, sets, reps,"
                    " weight_from, effort")
            .eq("user_id", user_id)
            .gte("session_date", (monday - timedelta(days=_PROGRESSION_DAYS)).isoformat())
            .lte("session_date", sunday.isoformat())
            .order("session_date", desc=True)
            .limit(1000)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return []
    return res.data or []


@router.get("/week/current")
def get_current_week(scope: AgentScope = Depends(resolve_agent_scope)) -> dict:
    """Veckan som innehåller dagens datum (ur MASTER planned_sessions)."""
    return _week_plan(get_postgrest(), scope.user_id, _monday_of(clock.today()))


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
    start = (since or (clock.today() - timedelta(days=28))).isoformat()
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
    # Samma källdedup som dashboarden. Utan den såg coachen tp+strava-
    # dubbletter av samma pass och resonerade om dubbel volym (docs/12 G4).
    return {"since": start, "sessions": dedup_cross_source(res.data or [])}


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
    # Tomt svar är normalläget för en adept utan kopplad klocka, inte ett fel.
    # Noten säger det rakt ut så att en agent slutar leta efter data som aldrig
    # kommer och planerar på veckoram och upplevd ansträngning i stället.
    no_watch = (
        "Ingen kopplad klocka — adepten har ingen återhämtningsdata. Planera på "
        "veckoram och erfarenhetsnivå ur get_athlete, ramp försiktigt och fråga "
        "adepten hur passen kändes."
    )
    if not gid:
        return {"metrics": [], "has_data": False, "note": no_watch}
    res = (
        client.schema("garmin_coach").table("daily_metrics")
        .select("metric_date, resting_hr, hrv_last_night_ms, hrv_baseline_low,"
                " hrv_baseline_high, sleep_score, readiness_score, stress_avg, load_ratio")
        .eq("athlete_id", gid)
        .order("metric_date", desc=True)
        .limit(days)
        .execute()
    )
    metrics = res.data or []
    if not metrics:
        return {
            "metrics": [], "has_data": False,
            "note": (
                "Klocka kopplad men inga dygn synkade i perioden — behandla som "
                "avsaknad av data, inte som goda värden."
            ),
        }
    return {"metrics": metrics, "has_data": True}


# ---------- skriva: plan (Nils vinner) ----------


class PlanSessionIn(BaseModel):
    date: date_type
    sport: str = Field(..., description="bike/run/swim/strength/rest (eller svenska)")
    title: str
    duration_min: int | None = None
    intensity: str = ""
    details: str = ""
    workout_code: str = ""
    # Strukturerad övningslista utöver prosan i details. Loggformuläret
    # förifylls från den, så adepten bekräftar i stället för att skriva av.
    exercises: list[dict] = Field(default_factory=list)


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
        "exercises": normalize_exercises(body.exercises) or None,
        # Coachens pass ersätter motorns helt. Lämnades steps kvar från en
        # övertagen trixa2-rad föll loggformuläret tillbaka på dem — adepten
        # såg motorns knäböj under coachens "Rörlighet 20 min", medan
        # get_week visade en tom lista.
        "steps": None,
        "purpose": None,
        "status": "planned",
        "origin": "nils",
    }

    warnings = _plan_warnings(sport_sv, row)

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
            "replaced_origin": previous, "warnings": warnings,
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
            "replaced_origin": None, "warnings": warnings}


def _plan_warnings(sport_sv: str, row: dict) -> list[str]:
    """Vad coachen behöver veta om passet hen just skrev.

    Ett styrkepass utan ``exercises`` ser komplett ut för coachen — övningarna
    står ju i ``details`` — men landar hos adepten som ett tomt loggformulär,
    och utan loggad vikt har lastprogressionen inget att räkna nästa pass på.
    Verktygsbeskrivningen sade redan att listan ska skickas; det räckte inte,
    så skrivningen svarar numera med vad som saknas.

    Varning, inte avslag: ett "Rörlighet 20 min" som lagts som Styrka är ett
    giltigt pass utan set och reps att bocka av.
    """
    if sport_sv != "Styrka":
        return []
    exercises = row.get("exercises") or []
    if not exercises:
        return [
            "Styrkepasset saknar 'exercises'. Adepten får ett tomt loggformulär "
            "och måste skriva in varje övningsnamn för hand, och utan loggad "
            "vikt kan lastprogressionen inte räkna nästa pass. Skicka om passet "
            "med övningarna som lista."
        ]
    missing = [
        ex.get("name") for ex in exercises
        if ex.get("reps_min") is None or ex.get("reps_max") is None
    ]
    if missing:
        return [
            "Övningarna saknar repspann (reps_min/reps_max): "
            + ", ".join(str(n) for n in missing[:6])
            + ". Progressionen antar då spannet reps till reps+2, vilket kan "
            "växla till tyngre vikt tidigare än protokollet avser."
        ]
    return []


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
    if body.planned_session_id:
        # Främmande nyckeln godtar vilken adepts pass som helst. Utan kollen
        # kunde en token för adept A hänga en override — med medicinsk
        # kontext — på adept B:s pass.
        own = (
            client.table("planned_sessions").select("id")
            .eq("id", body.planned_session_id).eq("user_id", scope.user_id)
            .limit(1).execute()
        )
        if not own.data:
            raise HTTPException(404, "planned_session_id tillhör inte adepten.")

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
