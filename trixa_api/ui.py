"""HTML-formulär och vyer för Trixa.

Tunt skal: använder samma logik som API:t men returnerar Jinja-renderad HTML.
Auth: separat — för adept-UI används samma Bearer-token i cookie (MVP).
För riktig deploy: byt till Supabase JWT med signing.
"""

from __future__ import annotations

import json
import os
from datetime import date as date_type, timedelta
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from coach.trixa.db import get_postgrest
from coach.trixa.planner import (
    generate_week,
    list_workout_alternatives,
    swap_workout_code,
    swap_workout_discipline_and_replan,
)


router = APIRouter(prefix="/ui", tags=["ui"])

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Använder Jinja2 direkt (inte starlette's Jinja2Templates) för att
# kringgå en cache-bugg i kombinationen Jinja2 3.1.6 + Python 3.14.
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)


def _render(template_name: str, context: dict) -> HTMLResponse:
    """Direkt-rendering utan starlette-wrapper."""
    template = _jinja_env.get_template(template_name)
    html = template.render(**context)
    return HTMLResponse(content=html)


# Default-adept-id för MVP — Niklas. När vi har auth byts detta till cookien.
_DEFAULT_USER_ID = os.environ.get(
    "TRIXA_DEFAULT_USER_ID", "09db449d-b8fd-409a-b475-3401b0de9858"
)


def _current_user_id(request: Request) -> str:
    """Hämta aktiv adept-id. För MVP: env-default. Senare: JWT/cookie."""
    return request.cookies.get("trixa_user_id") or _DEFAULT_USER_ID


def _monday_of(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


# ---------- Dashboard ----------


def _enrich_with_alternatives(week_data: dict | None, phase: str, period: str | None) -> None:
    """Lägg till alternative-listor per pass för 'byt ut'-dropdown."""
    if not week_data or not week_data.get("workouts"):
        return
    for w in week_data["workouts"]:
        if w["category"] and w["sport"] in ("swim", "bike", "run"):
            alts = list_workout_alternatives(
                category=w["category"],
                discipline=w["sport"],
                phase=phase,
                period=period,
                exclude_code=w["code"],
            )
            w["alternatives"] = [{"code": a["code"], "name": a["name"]} for a in alts]
        else:
            w["alternatives"] = []


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()

    a_res = client.table("athlete_profiles").select("*").eq("user_id", user_id).execute()
    athlete = a_res.data[0] if a_res.data else None

    if not athlete:
        return _render("dashboard.html", {
            "request": request, "athlete": None,
            "this_week": None, "next_week": None, "alerts": [],
        })

    # Hämta båda veckorna från DB
    today = date_type.today()
    this_monday = _monday_of(today)
    next_monday = this_monday + timedelta(days=7)
    this_iso = this_monday.isocalendar()
    next_iso = next_monday.isocalendar()

    this_week = _fetch_current_week_data(client, athlete["id"], this_iso[0], this_iso[1])
    next_week = _fetch_current_week_data(client, athlete["id"], next_iso[0], next_iso[1])

    # Hämta alerts
    alerts_res = (
        client.table("coach_alerts")
        .select("*")
        .eq("athlete_id", user_id)
        .eq("is_dismissed", False)
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )

    # Lägg på namn för välkomst
    name_res = client.table("profiles").select("name").eq("id", user_id).execute()
    if name_res.data:
        athlete["name"] = name_res.data[0].get("name")

    # Hämta engine-fas för alternative-uppslag
    from coach.trixa.planner import _build_athlete_state, _build_ot_signals, _run_engine
    state = _build_athlete_state(athlete, None, today)
    decisions = _run_engine(state, _build_ot_signals(athlete, None), 1, 6)
    phase = decisions["phase_recommendation"]["phase"]
    period = decisions["phase_recommendation"]["period"]

    # Berika båda veckorna med alternativ-listor
    _enrich_with_alternatives(this_week, phase, period)
    _enrich_with_alternatives(next_week, phase, period)

    return _render("dashboard.html", {
        "request": request,
        "athlete": athlete,
        "this_week": this_week,
        "next_week": next_week,
        "alerts": alerts_res.data or [],
        "phase": phase,
        "this_monday": this_monday.isoformat(),
        "next_monday": next_monday.isoformat(),
    })


def _fetch_current_week_data(client, athlete_id: str, year: int, week_num: int) -> dict | None:
    plan_res = (
        client.table("training_plans")
        .select("id")
        .eq("athlete_id", athlete_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not plan_res.data:
        return None
    plan_id = plan_res.data[0]["id"]

    week_res = (
        client.table("training_weeks")
        .select("*")
        .eq("plan_id", plan_id)
        .eq("year", year)
        .eq("week_number", week_num)
        .limit(1)
        .execute()
    )
    if not week_res.data:
        return None
    week = week_res.data[0]
    # Lägg på beräknat week_start (måndag i ISO-veckan) för UI-actions
    week["week_start"] = date_type.fromisocalendar(year, week_num, 1).isoformat()

    workouts_res = (
        client.table("workouts")
        .select("*")
        .eq("week_id", week["id"])
        .order("date")
        .execute()
    )
    workouts = workouts_res.data or []
    # Mappa workouts till template-vänligt format med DB-id för edit-actions
    week["workouts"] = []
    for w in workouts:
        code = w.get("title_simple") or w["title"]
        # Härled kategori från koden (format: <CAT><N>_<disc>_<NN> eller <CAT>_<disc>_template)
        category = code.split("_")[0][:2] if "_" in code else ""
        week["workouts"].append({
            "id": w["id"],
            "date": w["date"],
            "sport": w["sport"],
            "title": w["title"],
            "code": code,
            "category": category,
            "duration_minutes": w.get("duration_minutes") or 0,
            "intensity": w.get("intensity") or "",
            "notes": w.get("notes") or "",
            "steps": w.get("steps") or [],
            "coach_notes": w.get("coach_notes") or "",
        })
    return week


# ---------- Plan-preview ----------


@router.get("/plan", response_class=HTMLResponse)
def plan_view(request: Request) -> HTMLResponse:
    """Visa nästa veckas plan (dry-run-rendering — skriver inte till DB)."""
    user_id = _current_user_id(request)
    next_monday = _monday_of(date_type.today() + timedelta(days=7))
    try:
        plan = generate_week(
            athlete_user_id=user_id,
            week_start=next_monday,
            dry_run=True,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    return _render("plan.html", {"request": request, "plan": plan})


# ---------- Weekly report ----------


@router.get("/report", response_class=HTMLResponse)
def report_form(request: Request) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()

    a_res = client.table("athlete_profiles").select("id").eq("user_id", user_id).execute()
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a_res.data[0]["id"]

    week_start = _monday_of(date_type.today())
    existing_res = (
        client.table("weekly_reports")
        .select("*")
        .eq("athlete_id", athlete_id)
        .eq("week_start", week_start.isoformat())
        .execute()
    )
    existing = existing_res.data[0] if existing_res.data else None

    return _render("report.html", {
            "request": request,
            "week_start": week_start.isoformat(),
            "existing": existing,
            "submitted": False,
        })


@router.post("/report", response_class=HTMLResponse)
def report_submit(
    request: Request,
    week_start: str = Form(...),
    sleep_quality: int | None = Form(None),
    motivation: int | None = Form(None),
    soreness: int | None = Form(None),
    energy: int | None = Form(None),
    stress: int | None = Form(None),
    pain_present: str | None = Form(None),
    injury_change: str | None = Form(None),
    illness_present: str | None = Form(None),
    travel_planned: str | None = Form(None),
    notes: str = Form(""),
) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()

    a_res = client.table("athlete_profiles").select("id").eq("user_id", user_id).execute()
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a_res.data[0]["id"]

    row = {
        "athlete_id": athlete_id,
        "week_start": week_start,
        "sleep_quality": sleep_quality,
        "motivation": motivation,
        "soreness": soreness,
        "energy": energy,
        "stress": stress,
        "pain_present": pain_present == "1",
        "injury_change": injury_change == "1",
        "illness_present": illness_present == "1",
        "travel_planned": travel_planned == "1",
        "notes": notes or "",
    }
    client.table("weekly_reports").upsert(
        row, on_conflict="athlete_id,week_start"
    ).execute()

    # Rendera back-form med "submitted=True"-banner
    existing_res = (
        client.table("weekly_reports")
        .select("*")
        .eq("athlete_id", athlete_id)
        .eq("week_start", week_start)
        .execute()
    )
    return _render("report.html", {
            "request": request,
            "week_start": week_start,
            "existing": existing_res.data[0] if existing_res.data else None,
            "submitted": True,
        })


# ---------- Admin ----------


@router.get("/admin", response_class=HTMLResponse)
def admin_view(request: Request) -> HTMLResponse:
    user_id = _current_user_id(request)
    next_monday = _monday_of(date_type.today())
    return _render("admin.html", {
            "request": request,
            "default_user_id": user_id,
            "default_week_start": next_monday.isoformat(),
            "result": None,
            "result_json": None,
        })


_DAY_LABELS = [
    ("monday", "Måndag"),
    ("tuesday", "Tisdag"),
    ("wednesday", "Onsdag"),
    ("thursday", "Torsdag"),
    ("friday", "Fredag"),
    ("saturday", "Lördag"),
    ("sunday", "Söndag"),
]


# ---------- Settings (adept-prefs för veckans skelett) ----------


_SPORT_OPTIONS = [
    ("swim", "Simning"),
    ("bike", "Cykel"),
    ("run", "Löpning"),
    ("strength", "Styrketräning"),
]


_BODY_LOCATIONS = [
    ("lower_back", "Korsrygg"),
    ("upper_back", "Övre rygg"),
    ("neck", "Nacke"),
    ("shoulder_left", "Axel vänster"),
    ("shoulder_right", "Axel höger"),
    ("elbow_left", "Armbåge vänster"),
    ("elbow_right", "Armbåge höger"),
    ("wrist_left", "Handled vänster"),
    ("wrist_right", "Handled höger"),
    ("biceps_left", "Biceps vänster"),
    ("biceps_right", "Biceps höger"),
    ("chest", "Bröst"),
    ("abs", "Mage"),
    ("hip_left", "Höft vänster"),
    ("hip_right", "Höft höger"),
    ("glute_left", "Säte vänster"),
    ("glute_right", "Säte höger"),
    ("quad_left", "Lår framsida vänster"),
    ("quad_right", "Lår framsida höger"),
    ("hamstring_left", "Lår baksida vänster"),
    ("hamstring_right", "Lår baksida höger"),
    ("knee_left", "Knä vänster"),
    ("knee_right", "Knä höger"),
    ("calf_left", "Vad vänster"),
    ("calf_right", "Vad höger"),
    ("achilles_left", "Hälsena vänster"),
    ("achilles_right", "Hälsena höger"),
    ("ankle_left", "Fotled vänster"),
    ("ankle_right", "Fotled höger"),
    ("foot_left", "Fot vänster"),
    ("foot_right", "Fot höger"),
    ("systemic", "Systemisk (stress, sjukdom, allmäntillstånd)"),
    ("other", "Annat"),
]


_DISCIPLINES_FOR_IMPACT = [
    ("swim", "Simning"),
    ("bike", "Cykel"),
    ("run", "Löpning"),
    ("strength", "Styrka"),
]


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, saved: bool = False) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select(
            "sports, long_bike_day, long_run_day, preferred_rest_days,"
            " equipment, preferred_settings"
        )
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete = a_res.data[0]
    athlete["preferred_rest_days"] = athlete.get("preferred_rest_days") or []
    athlete["sports"] = athlete.get("sports") or ["swim", "bike", "run"]
    athlete["equipment"] = athlete.get("equipment") or {}
    athlete["preferred_settings"] = athlete.get("preferred_settings") or {}
    return _render(
        "settings.html",
        {
            "request": request,
            "athlete": athlete,
            "days": _DAY_LABELS,
            "sports_options": _SPORT_OPTIONS,
            "disciplines_for_setting": [
                ("swim", "Simning"),
                ("bike", "Cykel"),
                ("run", "Löpning"),
            ],
            "saved": saved,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
def settings_submit(
    request: Request,
    sports: list[str] = Form(default=[]),
    long_bike_day: str = Form(""),
    long_run_day: str = Form(""),
    rest_days: list[str] = Form(default=[]),
    has_trainer: str | None = Form(None),
    has_treadmill: str | None = Form(None),
    has_power_meter_bike: str | None = Form(None),
    has_power_meter_run: str | None = Form(None),
    hr_strap: str | None = Form(None),
    pool_type: str = Form("25m"),
    setting_swim: str = Form("any"),
    setting_bike: str = Form("any"),
    setting_run: str = Form("any"),
) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("id")
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a_res.data[0]["id"]

    # Validera sports — minst en disciplin måste vara aktiv
    valid_sports = [s for s in sports if s in {"swim", "bike", "run", "strength"}]
    if not valid_sports:
        valid_sports = ["swim", "bike", "run"]

    update = {
        "sports": valid_sports,
        "long_bike_day": long_bike_day or None,
        "long_run_day": long_run_day or None,
        "preferred_rest_days": rest_days,
        "equipment": {
            "has_trainer": has_trainer == "1",
            "has_treadmill": has_treadmill == "1",
            "has_power_meter_bike": has_power_meter_bike == "1",
            "has_power_meter_run": has_power_meter_run == "1",
            "hr_strap": hr_strap == "1",
            "pool_type": pool_type,
        },
        "preferred_settings": {
            "swim": setting_swim if setting_swim in {"any", "indoor", "outdoor"} else "any",
            "bike": setting_bike if setting_bike in {"any", "indoor", "outdoor"} else "any",
            "run": setting_run if setting_run in {"any", "indoor", "outdoor"} else "any",
        },
    }
    client.table("athlete_profiles").update(update).eq("id", athlete_id).execute()

    return settings_view(request, saved=True)


# ---------- Hälsa (strukturerad skaderapport) ----------


@router.get("/health", response_class=HTMLResponse)
def health_view(request: Request, added: bool = False) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("id, active_concerns")
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete = a_res.data[0]
    concerns = athlete.get("active_concerns") or []
    return _render(
        "health.html",
        {
            "request": request,
            "concerns": concerns,
            "locations": _BODY_LOCATIONS,
            "disciplines": _DISCIPLINES_FOR_IMPACT,
            "added": added,
        },
    )


@router.post("/health/add", response_class=HTMLResponse)
def health_add(
    request: Request,
    name: str = Form(...),
    location: str = Form(""),
    severity: int = Form(2),
    since_date: str = Form(""),
    impact_swim: str = Form("none"),
    impact_bike: str = Form("none"),
    impact_run: str = Form("none"),
    impact_strength: str = Form("none"),
    needs_followup: str | None = Form(None),
    follow_up_by: str = Form(""),
    notes: str = Form(""),
) -> Any:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("id, active_concerns")
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a_res.data[0]["id"]
    concerns = a_res.data[0].get("active_concerns") or []

    new_concern = {
        "name": name,
        "location": location or None,
        "severity": severity,
        "since_date": since_date or None,
        "needs_followup": needs_followup == "1",
        "follow_up_by": follow_up_by or None,
        "notes": notes or None,
        "impact_per_discipline": {
            "swim": impact_swim,
            "bike": impact_bike,
            "run": impact_run,
            "strength": impact_strength,
        },
    }
    concerns.append(new_concern)
    client.table("athlete_profiles").update(
        {"active_concerns": concerns}
    ).eq("id", athlete_id).execute()

    return RedirectResponse(url="/ui/health?added=true", status_code=303)


@router.post("/health/remove", response_class=HTMLResponse)
def health_remove(
    request: Request,
    index: int = Form(...),
) -> Any:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("id, active_concerns")
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    athlete_id = a_res.data[0]["id"]
    concerns = a_res.data[0].get("active_concerns") or []

    if 0 <= index < len(concerns):
        concerns.pop(index)
        client.table("athlete_profiles").update(
            {"active_concerns": concerns}
        ).eq("id", athlete_id).execute()

    return RedirectResponse(url="/ui/health", status_code=303)


# ---------- Adept-actions: regenerera vecka, byt pass, byt gren ----------


@router.post("/plan/regenerate", response_class=HTMLResponse)
def plan_regenerate(request: Request, week_start: str = Form(...)) -> Any:
    """Regenerera hela veckan från engine + passbank. Skriver över befintlig plan."""
    user_id = _current_user_id(request)
    try:
        ws = date_type.fromisoformat(week_start)
        generate_week(
            athlete_user_id=user_id,
            week_start=ws,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Genereringsfel: {exc}")
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/workouts/{workout_id}/swap", response_class=HTMLResponse)
def workout_swap(
    request: Request,
    workout_id: str,
    new_code: str = Form(...),
) -> Any:
    """Byt ut ett pass mot ett annat från passbanken (samma kategori/disciplin)."""
    try:
        swap_workout_code(workout_id, new_code)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/workouts/{workout_id}/swap-discipline", response_class=HTMLResponse)
def workout_swap_discipline(
    request: Request,
    workout_id: str,
    new_discipline: str = Form(...),
) -> Any:
    """Byt en specifik dag till annan disciplin och planera om resten av veckan."""
    if new_discipline not in ("swim", "bike", "run"):
        raise HTTPException(400, "new_discipline måste vara swim, bike eller run")
    try:
        swap_workout_discipline_and_replan(workout_id, new_discipline)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/admin/generate", response_class=HTMLResponse)
def admin_generate(
    request: Request,
    athlete_user_id: str = Form(...),
    week_start: str = Form(...),
    week_in_period: int = Form(1),
    weeks_in_period: int = Form(6),
    apply: str | None = Form(None),
) -> HTMLResponse:
    try:
        ws = date_type.fromisoformat(week_start)
        plan = generate_week(
            athlete_user_id=athlete_user_id,
            week_start=ws,
            dry_run=(apply != "1"),
            week_in_period=week_in_period,
            weeks_in_period=weeks_in_period,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Genereringsfel: {exc}")

    result_dict = plan.to_dict()
    return _render("admin.html", {
            "request": request,
            "default_user_id": athlete_user_id,
            "default_week_start": week_start,
            "result": plan,
            "result_json": json.dumps(result_dict, indent=2, ensure_ascii=False, default=str),
        })
