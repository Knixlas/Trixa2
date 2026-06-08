"""Trixa-API — FastAPI-skal.

Kör lokalt:
    uvicorn trixa_api.main:app --reload

Kör produktion (Railway):
    uvicorn trixa_api.main:app --host 0.0.0.0 --port $PORT

Auth: Bearer-token från env TRIXA_API_TOKEN. Sätt TRIXA_ALLOW_NO_AUTH=1
för lokal dev utan auth (osäkert för produktion).
"""

from __future__ import annotations

import os
from datetime import date as date_type
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from coach.engine.loader import load_workouts, load_drills
from coach.trixa.db import get_postgrest
from coach.trixa.planner import generate_week, render_plan_markdown
from trixa_api import supabase_auth
from trixa_api.auth import require_api_token
from trixa_api.ui import (
    router as ui_router,
    _DEFAULT_USER_ID,
    set_session_cookies,
    is_secure_request,
)
from trixa_api.schemas import (
    AlertResponse,
    AthleteResponse,
    CoachOverrideRequest,
    CoachOverrideResponse,
    GeneratePlanRequest,
    WeekPlanResponse,
    WeeklyReportRequest,
    WorkoutSummary,
)


app = FastAPI(
    title="Trixa API",
    description="Deterministisk triathlontränare för Niklas Svidén och andra adepter.",
    version="0.1.0",
)

# CORS — för adept-UI och Nils mobil-tråd
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: snäva åt vid go-live
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

# Mounta UI-routes (publikt — egen auth kommer i framtid)
app.include_router(ui_router)

# Servera temat (trixa.css m.m.) — tropical-temats stylesheet ligger i trixa_api/static
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Skyddar adept-webben (/ui): kräver giltig Supabase-session, annars → /ui/login.

    /api har egen token-auth, /ui/login + /ui/logout + /static + /health är öppna.
    Förnyar utgången access_token tyst via refresh-token. Dev-escape:
    TRIXA_ALLOW_NO_AUTH=1 (aldrig i prod).
    """
    request.state.user_id = None
    path = request.url.path
    gated = path.startswith("/ui") and not path.startswith(
        ("/ui/login", "/ui/logout", "/ui/signup")
    )
    if not gated:
        return await call_next(request)

    if os.environ.get("TRIXA_ALLOW_NO_AUTH") == "1":
        request.state.user_id = _DEFAULT_USER_ID
        return await call_next(request)

    access = request.cookies.get("sb_access")
    refresh = request.cookies.get("sb_refresh")
    uid = await run_in_threadpool(supabase_auth.get_user_id, access) if access else None
    renewed = None
    if uid is None and refresh:
        renewed = await run_in_threadpool(supabase_auth.refresh_session, refresh)
        if renewed:
            uid = renewed.get("user_id")
    if not uid:
        return RedirectResponse(url="/ui/login", status_code=303)

    request.state.user_id = uid
    response = await call_next(request)
    if renewed:
        set_session_cookies(response, renewed, secure=is_secure_request(request))
    return response


@app.get("/")
def root_redirect() -> Any:
    """Root pekar mot dashboard."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/ui/")


# ---------- Health (publikt) ----------


@app.get("/health")
def health() -> dict:
    """Railway hälsokoll. Returnerar 200 utan DB-koll så svaret är snabbt."""
    return {"status": "ok", "service": "trixa-api"}


@app.get("/health/db")
def health_db() -> dict:
    """Verifierar att Supabase-kopplingen funkar."""
    try:
        client = get_postgrest()
        res = client.table("athlete_profiles").select("id", count="exact").limit(1).execute()
        return {"status": "ok", "athlete_profiles_count": res.count}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"DB-koppling misslyckades: {exc}",
        )


# ---------- Athlete ----------


@app.get(
    "/api/athlete/{user_id}",
    response_model=AthleteResponse,
    dependencies=[Depends(require_api_token)],
)
def get_athlete(user_id: str) -> AthleteResponse:
    """Hämta full athlete-state för en adept (Nils läser för kontextbygge)."""
    client = get_postgrest()
    res = client.table("athlete_profiles").select("*").eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(404, f"Ingen athlete_profiles-rad för user_id={user_id}")
    a = res.data[0]
    return AthleteResponse(
        id=a["id"],
        user_id=a["user_id"],
        goal=a.get("goal") or "",
        sports=a.get("sports") or [],
        experience_level=a.get("experience_level") or "",
        weekly_hours=float(a.get("weekly_hours") or 0),
        race_type=a.get("race_type"),
        race_date=a.get("race_date"),
        time_goal=a.get("time_goal"),
        ftp=a.get("ftp"),
        lthr=a.get("lthr"),
        swim_css=a.get("swim_css"),
        run_threshold_pace=a.get("run_threshold_pace"),
        health_conditions=a.get("health_conditions") or [],
        active_concerns=a.get("active_concerns") or [],
        medications=a.get("medications") or [],
        phase_state=a.get("phase_state") or {},
        notes=a.get("notes") or "",
    )


# ---------- Plan ----------

# planned_sessions använder svenska sportnamn; API svarar med Trixas discipliner.
_SV_EN_SPORT = {
    "Cykel": "bike", "Cykling": "bike", "Löpning": "run", "Lopning": "run",
    "Sim": "swim", "Simning": "swim", "Styrka": "strength", "Vila": "rest",
    "Brick": "brick", "Yoga": "rest", "Promenad": "rest",
}


@app.get(
    "/api/week/current",
    response_model=WeekPlanResponse | None,
    dependencies=[Depends(require_api_token)],
)
def get_current_week(
    athlete_user_id: str = Query(..., description="auth.users.id för adepten"),
) -> WeekPlanResponse | None:
    """Hämta veckan som innehåller dagens datum för en adept.

    MASTER: läser planen från public.planned_sessions (docs/08). Faller tillbaka
    på engine-tabellen workouts om ingen planned_sessions-rad finns för veckan.
    """
    client = get_postgrest()
    athlete_res = (
        client.table("athlete_profiles")
        .select("id, user_id")
        .eq("user_id", athlete_user_id)
        .execute()
    )
    athlete_id = athlete_res.data[0]["id"] if athlete_res.data else None

    today = date_type.today()
    iso_year, iso_week, _ = today.isocalendar()
    week_start = date_type.fromisocalendar(iso_year, iso_week, 1)
    week_end = date_type.fromisocalendar(iso_year, iso_week, 7)

    # 1) MASTER: planned_sessions (Nils/Trixa2). Nyckel = user_id.
    ps_res = (
        client.table("planned_sessions")
        .select("date, sport, title, workout_code, intensity, duration_min, details, purpose")
        .eq("user_id", athlete_user_id)
        .gte("date", week_start.isoformat())
        .lte("date", week_end.isoformat())
        .order("date")
        .execute()
    )
    if ps_res.data:
        workouts = [
            WorkoutSummary(
                date=w["date"],
                sport=_SV_EN_SPORT.get(w.get("sport"), (w.get("sport") or "").lower()),
                code=w.get("workout_code") or w.get("title") or "",
                title=w.get("title") or "",
                category=w.get("purpose") or "",
                duration_minutes=int(w.get("duration_min") or 0),
                intensity=w.get("intensity") or "",
                notes=w.get("details") or "",
            )
            for w in ps_res.data
        ]
        return WeekPlanResponse(
            athlete_id=athlete_id,
            athlete_user_id=athlete_user_id,
            week_start=week_start,
            phase="",
            period=None,
            total_hours_target=0.0,
            discipline_hours={},
            categories=[],
            strength_protocol="",
            overtraining_level="",
            overtraining_flags=[],
            plan_adjustment=None,
            workouts=workouts,
            warnings=[],
            persisted_week_id=None,
        )

    # Ingen plan i mastern (planned_sessions) för veckan.
    return None


@app.post(
    "/api/plan/generate",
    response_model=WeekPlanResponse,
    dependencies=[Depends(require_api_token)],
)
def post_generate_plan(req: GeneratePlanRequest) -> WeekPlanResponse:
    """Trigga generering av en konkret veckoplan.

    Default är dry-run (apply=False). Sätt apply=True för att skriva till DB
    + producera alerts.
    """
    try:
        plan = generate_week(
            athlete_user_id=req.athlete_user_id,
            week_start=req.week_start,
            dry_run=not req.apply,
            week_in_period=req.week_in_period,
            weeks_in_period=req.weeks_in_period,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Genereringsfel: {exc}")

    return WeekPlanResponse(
        athlete_id=plan.athlete_id,
        athlete_user_id=plan.athlete_user_id,
        week_start=plan.week_start,
        phase=plan.phase,
        period=plan.period,
        total_hours_target=plan.total_hours_target,
        discipline_hours=plan.discipline_hours,
        categories=plan.categories,
        strength_protocol=plan.strength_protocol,
        overtraining_level=plan.overtraining_level,
        overtraining_flags=plan.overtraining_flags,
        plan_adjustment=plan.plan_adjustment,
        workouts=[
            WorkoutSummary(
                date=w.date,
                sport=w.sport,
                code=w.code,
                title=w.title,
                category=w.category,
                duration_minutes=w.duration_minutes,
                intensity=w.intensity,
                notes=w.notes,
            )
            for w in plan.workouts
        ],
        warnings=plan.warnings,
        persisted_week_id=plan.engine_decisions.get("persisted_week_id"),
        alerts_written=plan.engine_decisions.get("alerts_written", 0),
    )


@app.get(
    "/api/plan/markdown",
    dependencies=[Depends(require_api_token)],
)
def get_plan_markdown(
    athlete_user_id: str = Query(...),
    week_start: date_type = Query(...),
    week_in_period: int = Query(1, ge=1),
    weeks_in_period: int = Query(6, ge=1),
) -> dict:
    """Generera och returnera markdown-versionen av en veckoplan (dry-run).

    Användbart för Nils att läsa direkt utan att skriva till DB.
    """
    try:
        plan = generate_week(
            athlete_user_id=athlete_user_id,
            week_start=week_start,
            dry_run=True,
            week_in_period=week_in_period,
            weeks_in_period=weeks_in_period,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    return {"markdown": render_plan_markdown(plan)}


# ---------- Override ----------


@app.post(
    "/api/override",
    response_model=CoachOverrideResponse,
    dependencies=[Depends(require_api_token)],
)
def post_override(req: CoachOverrideRequest) -> CoachOverrideResponse:
    """Coach (Nils) skapar en override av engine-beslut.

    Trixa-planner läser aktiva overrides nästa gång den genererar veckan.
    """
    client = get_postgrest()

    # Hitta athlete_profiles.id
    a_res = (
        client.table("athlete_profiles")
        .select("id")
        .eq("user_id", req.athlete_user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, f"Athlete saknas: {req.athlete_user_id}")
    athlete_id = a_res.data[0]["id"]

    # Hitta coach_user_id via coach_athletes
    coach_res = (
        client.table("coach_athletes")
        .select("coach_id")
        .eq("athlete_id", req.athlete_user_id)
        .in_("status", ["accepted", "active"])
        .limit(1)
        .execute()
    )
    if not coach_res.data:
        raise HTTPException(404, "Ingen aktiv coach kopplad till adepten")
    coach_user_id = coach_res.data[0]["coach_id"]

    row = {
        "athlete_id": athlete_id,
        "coach_user_id": coach_user_id,
        "scope": req.scope,
        "week_id": req.week_id,
        "workout_id": req.workout_id,
        "engine_recommendation": req.engine_recommendation,
        "override_decision": req.override_decision,
        "motivation": req.motivation,
        "medical_context_disclosed": req.medical_context_disclosed,
        "athlete_explicit_request": req.athlete_explicit_request,
    }

    res = client.table("coach_overrides").insert(row).execute()
    if not res.data:
        raise HTTPException(500, "Override-insert returnerade ingen data")
    o = res.data[0]
    return CoachOverrideResponse(
        id=o["id"],
        athlete_id=o["athlete_id"],
        scope=o["scope"],
        motivation=o["motivation"],
        is_active=o["is_active"],
        created_at=o["created_at"],
    )


# ---------- Weekly report ----------


@app.post(
    "/api/weekly_report",
    dependencies=[Depends(require_api_token)],
)
def post_weekly_report(req: WeeklyReportRequest) -> dict:
    """Adept submittar veckorapport. UPSERT på (athlete, week_start)."""
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("id")
        .eq("user_id", req.athlete_user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, f"Athlete saknas: {req.athlete_user_id}")
    athlete_id = a_res.data[0]["id"]

    row = {
        "athlete_id": athlete_id,
        "week_start": req.week_start.isoformat(),
        "sleep_quality": req.sleep_quality,
        "motivation": req.motivation,
        "soreness": req.soreness,
        "energy": req.energy,
        "stress": req.stress,
        "pain_present": req.pain_present,
        "injury_change": req.injury_change,
        "illness_present": req.illness_present,
        "travel_planned": req.travel_planned,
        "pain_locations": [pl.model_dump(mode="json") for pl in req.pain_locations],
        "notes": req.notes,
    }
    res = (
        client.table("weekly_reports")
        .upsert(row, on_conflict="athlete_id,week_start")
        .execute()
    )
    return {"status": "ok", "id": res.data[0]["id"] if res.data else None}


# ---------- Alerts ----------


@app.get(
    "/api/alerts",
    response_model=list[AlertResponse],
    dependencies=[Depends(require_api_token)],
)
def get_alerts(
    athlete_user_id: str = Query(...),
    include_dismissed: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
) -> list[AlertResponse]:
    """Lista senaste alerts för en adept."""
    client = get_postgrest()
    q = (
        client.table("coach_alerts")
        .select("*")
        .eq("athlete_id", athlete_user_id)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if not include_dismissed:
        q = q.eq("is_dismissed", False)
    res = q.execute()
    return [
        AlertResponse(
            id=a["id"],
            alert_type=a["alert_type"],
            severity=a.get("severity") or "info",
            title=a["title"],
            body=a["body"],
            data=a.get("data") or {},
            is_read=a.get("is_read") or False,
            is_dismissed=a.get("is_dismissed") or False,
            created_at=a["created_at"],
        )
        for a in (res.data or [])
    ]


# ---------- Workouts (passbank-uppslag) ----------


_WORKOUT_INDEX: dict[str, dict] | None = None


def _build_workout_index() -> dict[str, dict]:
    global _WORKOUT_INDEX
    if _WORKOUT_INDEX is None:
        _WORKOUT_INDEX = {w["code"]: w for w in load_workouts()}
    return _WORKOUT_INDEX


@app.get(
    "/api/workouts/{code}",
    dependencies=[Depends(require_api_token)],
)
def get_workout(code: str) -> dict:
    """Uppslag i passbanken. Returnerar full pass-definition."""
    idx = _build_workout_index()
    w = idx.get(code)
    if not w:
        raise HTTPException(404, f"Pass saknas: {code}")
    return w


@app.get(
    "/api/workouts",
    dependencies=[Depends(require_api_token)],
)
def list_workouts(
    discipline: str | None = Query(None),
    category: str | None = Query(None),
    phase: str | None = Query(None),
) -> dict:
    """Lista pass i passbanken med valfria filter."""
    workouts = list(_build_workout_index().values())
    if discipline:
        workouts = [w for w in workouts if w.get("discipline") == discipline]
    if category:
        workouts = [w for w in workouts if w.get("category") == category]
    if phase:
        workouts = [w for w in workouts if phase in (w.get("phase_appropriate") or [])]
    return {
        "count": len(workouts),
        "workouts": [
            {
                "code": w["code"],
                "name": w["name"],
                "discipline": w["discipline"],
                "category": w["category"],
                "type_code": w.get("type_code"),
                "phase_appropriate": w.get("phase_appropriate") or [],
            }
            for w in workouts
        ],
    }
