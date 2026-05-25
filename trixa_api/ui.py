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
from coach.trixa.planner import generate_week


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


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()

    a_res = client.table("athlete_profiles").select("*").eq("user_id", user_id).execute()
    athlete = a_res.data[0] if a_res.data else None

    if not athlete:
        return _render("dashboard.html", {"request": request, "athlete": None, "week": None, "alerts": []})

    # Hämta veckans plan från DB
    today = date_type.today()
    iso_year, iso_week, _ = today.isocalendar()
    week_data = _fetch_current_week_data(client, athlete["id"], iso_year, iso_week)

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

    return _render("dashboard.html", {
            "request": request,
            "athlete": athlete,
            "week": week_data,
            "alerts": alerts_res.data or [],
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

    workouts_res = (
        client.table("workouts")
        .select("*")
        .eq("week_id", week["id"])
        .order("date")
        .execute()
    )
    workouts = workouts_res.data or []
    # Mappa workouts till template-vänligt format
    week["workouts"] = [
        {
            "date": w["date"],
            "sport": w["sport"],
            "title": w["title"],
            "code": w.get("title_simple") or w["title"],
            "duration_minutes": w.get("duration_minutes") or 0,
            "intensity": w.get("intensity") or "",
            "notes": w.get("notes") or "",
        }
        for w in workouts
    ]
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
