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
from trixa_api import season, supabase_auth, readiness, strava_client


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


def _current_user_id(request: Request) -> str | None:
    """Inloggad adept-id. Sätts av auth-middleware (main.py) från Supabase-sessionen.
    None om ingen giltig session — skyddade /ui-routes når aldrig hit oinloggat."""
    return getattr(request.state, "user_id", None)


# ---------- Auth: Supabase-session via HttpOnly-cookies ----------


def set_session_cookies(response, session: dict, secure: bool = True) -> None:
    """Sätt access/refresh som HttpOnly-cookies. secure=True i prod (https)."""
    common = {"httponly": True, "samesite": "lax", "secure": secure, "path": "/"}
    if session.get("access_token"):
        response.set_cookie("sb_access", session["access_token"], max_age=3600, **common)
    if session.get("refresh_token"):
        response.set_cookie(
            "sb_refresh", session["refresh_token"], max_age=60 * 60 * 24 * 30, **common
        )


def clear_session_cookies(response) -> None:
    response.delete_cookie("sb_access", path="/")
    response.delete_cookie("sb_refresh", path="/")


def is_secure_request(request: Request) -> bool:
    """True om förfrågan kom via https. Respekterar X-Forwarded-Proto (Railway
    terminerar TLS i proxyn, så request.url.scheme är http internt)."""
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return _render("login.html", {"request": request, "error": ""})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
) -> Any:
    session = supabase_auth.sign_in_password(email.strip(), password)
    if not session or not session.get("user_id"):
        return _render(
            "login.html",
            {"request": request, "error": "Fel e-post eller lösenord — försök igen."},
        )
    resp = RedirectResponse(url="/ui/", status_code=303)
    set_session_cookies(resp, session, secure=is_secure_request(request))
    return resp


@router.get("/logout")
def logout(request: Request) -> Any:
    resp = RedirectResponse(url="/ui/login", status_code=303)
    clear_session_cookies(resp)
    return resp


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request) -> HTMLResponse:
    return _render("signup.html", {
        "request": request, "error": "",
        "require_code": bool(os.environ.get("TRIXA_SIGNUP_CODE")),
    })


@router.post("/signup")
def signup_submit(
    request: Request,
    name: str = Form(""),
    email: str = Form(...),
    password: str = Form(...),
    code: str = Form(""),
) -> Any:
    require_code = os.environ.get("TRIXA_SIGNUP_CODE")

    def _err(msg: str) -> HTMLResponse:
        return _render("signup.html", {
            "request": request, "error": msg, "require_code": bool(require_code),
        })

    if require_code and code.strip() != require_code:
        return _err("Fel eller saknad inbjudningskod.")
    if len(password) < 8:
        return _err("Lösenordet måste vara minst 8 tecken.")
    session, error = supabase_auth.sign_up(email.strip(), password, name.strip() or None)
    if error or not session:
        return _err(error or "Kunde inte skapa kontot.")
    resp = RedirectResponse(url="/ui/", status_code=303)
    set_session_cookies(resp, session, secure=is_secure_request(request))
    return resp


# ---------- Strava-koppling (utförda pass — robustare än Garmin) ----------


def _strava_redirect_uri(request: Request) -> str:
    scheme = "https" if is_secure_request(request) else request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}/ui/strava/callback"


@router.get("/strava/connect")
def strava_connect(request: Request) -> Any:
    uid = _current_user_id(request)
    if not uid:
        return RedirectResponse("/ui/login", status_code=303)
    if not strava_client.creds_configured():
        return RedirectResponse("/ui/settings?strava=noconfig", status_code=303)
    url = strava_client.authorize_url(
        _strava_redirect_uri(request), strava_client.sign_state(uid)
    )
    return RedirectResponse(url, status_code=303)


@router.get("/strava/callback")
def strava_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Any:
    uid = _current_user_id(request)
    if error or not code or strava_client.verify_state(state) != uid:
        return RedirectResponse("/ui/settings?strava=error", status_code=303)
    client = get_postgrest()
    try:
        tok = strava_client.exchange_code(code, _strava_redirect_uri(request))
        strava_client.save_tokens(
            client, uid, tok["access_token"], tok["refresh_token"],
            tok["expires_at"], (tok.get("athlete") or {}).get("id"), tok.get("scope"),
        )
        client.table("athlete_profiles").update({"use_strava": True}).eq("user_id", uid).execute()
        strava_client.sync_recent(client, uid, days=45)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/ui/settings?strava=error", status_code=303)
    return RedirectResponse("/ui/settings?strava=connected", status_code=303)


@router.post("/strava/sync")
def strava_sync(request: Request) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    try:
        strava_client.sync_recent(client, uid, days=45)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/ui/settings?strava=error", status_code=303)
    return RedirectResponse("/ui/settings?strava=synced", status_code=303)


@router.post("/strava/disconnect")
def strava_disconnect(request: Request) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    strava_client.delete_tokens(client, uid)
    client.table("athlete_profiles").update({"use_strava": False}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=disconnected", status_code=303)


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


# ---------- Säsongs-tidslinje (fas-staplar + följsamhet bakåt) ----------

# Vecko-cellernas följsamhetsfärger (mättare än fas-staplarna, så raden läses
# som en egen "hur gick det"-axel).
_COMPLIANCE_CELL = {"green": "#34d399", "yellow": "#fbbf24", "red": "#f87171"}
_COMPLIANCE_LABEL = {
    "green": "bra följsamhet", "yellow": "ok följsamhet", "red": "låg följsamhet",
}


def _compliance_by_week(client, plan_id, athlete_id, garmin_id, strava_user_id, today, user_id=None) -> dict:
    """Följsamhets-bucket per genererad, passerad vecka. Nyckel: (iso_year, iso_week)."""
    try:
        weeks_res = (
            client.table("training_weeks")
            .select("year, week_number")
            .eq("plan_id", plan_id)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for row in weeks_res.data or []:
        y, wn = row.get("year"), row.get("week_number")
        if y is None or wn is None:
            continue
        monday = date_type.fromisocalendar(y, wn, 1)
        if monday > today:
            continue  # framtida genererad vecka — ingen följsamhet att visa
        wk = _fetch_current_week_data(client, athlete_id, y, wn, garmin_id, strava_user_id, today, user_id)
        if wk and wk.get("workouts"):
            bucket = season.compliance_bucket(wk["workouts"], today)
            if bucket:
                out[(y, wn)] = bucket
    return out


def _decorate_timeline(timeline: dict, comp_map: dict, today, this_monday) -> None:
    """Lägg på vecko-cellernas färg/etikett (compliance bakåt, faint framåt)."""
    for w in timeline["weeks"]:
        bucket = comp_map.get((w["iso_year"], w["iso_week"]))
        w["is_current"] = w["monday"] == this_monday
        w["future"] = w["monday"] > today
        w["compliance"] = bucket  # rå bucket ("green"/"yellow"/"red"/None) för temat
        w["monday_iso"] = w["monday"].isoformat()
        if bucket:
            w["cell_bg"] = _COMPLIANCE_CELL[bucket]
            w["compliance_label"] = _COMPLIANCE_LABEL[bucket]
        elif w["monday"] <= today:
            w["cell_bg"] = "#d1d5db"  # passerad/pågående utan plan-data
            w["compliance_label"] = "ingen plan"
        else:
            w["cell_bg"] = "#eef2f7"  # framtid
            w["compliance_label"] = "planerad"


def _add_week_hours(by_week: dict, day_iso: str, hours: float) -> None:
    try:
        d = date_type.fromisoformat(str(day_iso)[:10])
    except (ValueError, TypeError):
        return
    iso = d.isocalendar()
    by_week[(iso[0], iso[1])] = by_week.get((iso[0], iso[1]), 0.0) + hours


def _weekly_hours_series(
    client, garmin_id, strava_user_id, today, weeks: int = 6
) -> list[float]:
    """Veckovolym (h) för de senaste `weeks` AVSLUTADE veckorna, äldst→nyast.

    Källagnostisk (Garmin/Strava), exkluderar innevarande (delvisa) vecka.
    Underlag för readiness-projektion (snitt) + ramp-vakt (trend).
    """
    this_mon = today - timedelta(days=today.weekday())
    start = (this_mon - timedelta(weeks=weeks)).isoformat()
    by_week: dict[tuple[int, int], float] = {}
    try:
        if garmin_id:
            res = (
                client.schema("garmin_coach").table("activities")
                .select("start_time, start_time_local, duration_sec")
                .eq("athlete_id", garmin_id).gte("start_time", start).execute()
            )
            for a in res.data or []:
                _add_week_hours(by_week, a.get("start_time_local") or a.get("start_time") or "",
                                (a.get("duration_sec") or 0) / 3600.0)
        elif strava_user_id:
            res = (
                client.table("strava_activities")
                .select("date, duration_min")
                .eq("user_id", strava_user_id).gte("date", start).execute()
            )
            for a in res.data or []:
                _add_week_hours(by_week, a.get("date") or "", (a.get("duration_min") or 0) / 60.0)
        else:
            return []
    except Exception:  # noqa: BLE001
        return []
    series: list[float] = []
    for i in range(weeks, 0, -1):
        iso = (this_mon - timedelta(weeks=i)).isocalendar()
        series.append(round(by_week.get((iso[0], iso[1]), 0.0), 1))
    return series


def _build_season_context(client, athlete, today, this_monday) -> dict | None:
    """Bygg säsongs-tidslinjen för dashboarden, eller None om ingen tävling."""
    race_raw = athlete.get("race_date")
    if not race_raw:
        return None
    try:
        race_d = date_type.fromisoformat(str(race_raw)[:10])
    except (ValueError, TypeError):
        return None
    timeline = season.build_phase_timeline(today, race_d)
    if not timeline:
        return None

    garmin_id = athlete.get("garmin_athlete_id")
    strava_user_id = None if garmin_id else athlete.get("user_id")

    comp_map: dict = {}
    try:
        plan_res = (
            client.table("training_plans")
            .select("id")
            .eq("athlete_id", athlete["id"])
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if plan_res.data:
            comp_map = _compliance_by_week(
                client, plan_res.data[0]["id"], athlete["id"],
                garmin_id, strava_user_id, today, athlete.get("user_id"),
            )
    except Exception:  # noqa: BLE001
        comp_map = {}

    _decorate_timeline(timeline, comp_map, today, this_monday)
    timeline["race_date"] = race_d.isoformat()
    timeline["race_label"] = (
        season.race_label(race_d) or (athlete.get("race_type") or "Tävling").capitalize()
    )

    # Readiness-projektion (skala upp säkert → när når man build, mot loppet?)
    # + ramp-vakt mot för skarp faktisk upptrappning.
    weeks_to_race = max((race_d - today).days // 7, 0)
    series = _weekly_hours_series(client, garmin_id, strava_user_id, today, weeks=6)
    recent = series[-4:] if series else []
    current_h = round(sum(recent) / len(recent), 1) if recent else 0.0
    proj = readiness.build_projection(current_h, weeks_to_race)
    timeline["readiness"] = {
        "current_hours": proj.current_hours,
        "base_eta": proj.base_eta,
        "build_eta": proj.build_eta,
        "ramp_pct": proj.ramp_pct,
        "on_track": proj.on_track,
        "verdict": proj.verdict,
        "ramp_flag": readiness.ramp_flag(series),
    }
    return timeline


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
            "timeline": None,
        })

    # Hämta båda veckorna från DB
    today = date_type.today()
    this_monday = _monday_of(today)
    next_monday = this_monday + timedelta(days=7)
    this_iso = this_monday.isocalendar()
    next_iso = next_monday.isocalendar()

    # Primärkälla per adept: har adepten valt Strava (use_strava) → Strava,
    # annars Garmin om kopplat, annars Strava (för Strava-only-vänner).
    uid = athlete.get("user_id")
    if athlete.get("use_strava"):
        garmin_id = None
        strava_user_id = uid
    else:
        garmin_id = athlete.get("garmin_athlete_id")
        strava_user_id = None if garmin_id else uid
    this_week = _fetch_current_week_data(
        client, athlete["id"], this_iso[0], this_iso[1], garmin_id, strava_user_id, today, uid
    )
    next_week = _fetch_current_week_data(
        client, athlete["id"], next_iso[0], next_iso[1], garmin_id, strava_user_id, today, uid
    )

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
    optimal_phase = decisions["phase_recommendation"].get("optimal_phase")
    behind = decisions["phase_recommendation"].get("behind", False)

    # Berika båda veckorna med alternativ-listor
    _enrich_with_alternatives(this_week, phase, period)
    _enrich_with_alternatives(next_week, phase, period)

    # Säsongs-tidslinje: fas-staplar bakåt från race + följsamhet per vecka
    timeline = _build_season_context(client, athlete, today, this_monday)

    return _render("dashboard.html", {
        "request": request,
        "athlete": athlete,
        "this_week": this_week,
        "next_week": next_week,
        "alerts": alerts_res.data or [],
        "phase": phase,
        "optimal_phase": optimal_phase,
        "behind": behind,
        "this_monday": this_monday.isoformat(),
        "next_monday": next_monday.isoformat(),
        "timeline": timeline,
    })


# ---------- Plan vs actual: matchning mot Garmin-aktiviteter ----------
#
# Ren, deterministisk matchning. Inga LLM-anrop. Statusen per pass räknas ut
# från passets datum vs idag + matchande aktivitet i garmin_coach.activities.

# Garmins activity_type → Trixas disciplin. Okända typer (other, multi_sport)
# mappas till None och matchar därför ingen planerad disciplin.
_ACTIVITY_SPORT_MAP = {
    "running": "run",
    "trail_running": "run",
    "treadmill_running": "run",
    "indoor_running": "run",
    "track_running": "run",
    "cycling": "bike",
    "road_biking": "bike",
    "mountain_biking": "bike",
    "gravel_cycling": "bike",
    "indoor_cycling": "bike",
    "virtual_ride": "bike",
    "swimming": "swim",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "strength": "strength",
    "strength_training": "strength",
}

# Strava activity_type → Trixas disciplin. Strava-tabellen lagrar mest svenska
# namn (gamla Trixas SPORT_MAP) men även råa engelska för otäckta typer.
# Allt som inte är swim/bike/run/strength → None (matchar ingen planerad disciplin).
_STRAVA_TYPE_TO_SPORT = {
    "Lopning": "run", "Löpning": "run", "Run": "run", "TrailRun": "run", "VirtualRun": "run",
    "Cykel": "bike", "Ride": "bike", "VirtualRide": "bike", "EBikeRide": "bike",
    "MountainBikeRide": "bike", "GravelRide": "bike",
    "Sim": "swim", "Swim": "swim", "OpenWaterSwim": "swim",
    "Styrka": "strength", "WeightTraining": "strength", "Workout": "strength",
}

# Statusdefinitioner: emoji + label + badge-färger. Färgerna ligger inline här
# (inte i base.html) för att hålla hela ändringen i UI-skiktets två filer.
_STATUS = {
    "done":        {"emoji": "🟢", "label": "Genomförd",          "bg": "#d1fae5", "fg": "#065f46"},
    "deviated":    {"emoji": "🟡", "label": "Avviken",            "bg": "#fef3c7", "fg": "#92400e"},
    "missed":      {"emoji": "🔴", "label": "Missad",             "bg": "#fee2e2", "fg": "#991b1b"},
    "planned":     {"emoji": "🔵", "label": "Planerad",           "bg": "#dbeafe", "fg": "#1e40af"},
    "today":       {"emoji": "⚪", "label": "Idag",               "bg": "#e5e7eb", "fg": "#374151"},
    "rest_ok":     {"emoji": "🟢", "label": "Vila hållen",        "bg": "#d1fae5", "fg": "#065f46"},
    "rest_broken": {"emoji": "🟡", "label": "Tränade på vilodag", "bg": "#fef3c7", "fg": "#92400e"},
}

_DURATION_TOLERANCE = 0.30  # ±30 % räknas som "genomförd som planerat"


def _activity_local_date(act: dict) -> str | None:
    """ISO-datumsträng (YYYY-MM-DD) för aktivitetens lokala starttid."""
    raw = act.get("start_time_local") or act.get("start_time")
    if not raw:
        return None
    return str(raw)[:10]


def _is_brick(code: str, sport: str) -> bool:
    """Brick-pass (cykel+löpning) matchar både cycling OCH running.

    Bricks finns inte i passbanken än, men kodprefixen är reserverade så att
    matchningen blir rätt den dag de läggs till.
    """
    if sport == "brick":
        return True
    c = (code or "").upper()
    return c.startswith(("BAE", "BTE", "BSS", "BME", "BMF", "BAC"))


def _sport_matches(plan_sport: str, code: str, activity_sport: str | None) -> bool:
    if activity_sport is None:
        return False
    if _is_brick(code, plan_sport):
        return activity_sport in ("bike", "run")
    return activity_sport == plan_sport


def _within_duration_tolerance(plan_min: float, actual_min: float) -> bool:
    """True om faktisk tid ligger inom ±30 % av planerad."""
    if not plan_min or plan_min <= 0:
        return True  # inget planerat tidsmått → bedöm bara på disciplin
    lo = plan_min * (1 - _DURATION_TOLERANCE)
    hi = plan_min * (1 + _DURATION_TOLERANCE)
    return lo <= actual_min <= hi


def _build_actual(act: dict, sport: str) -> dict:
    """Plocka ut faktiska siffror + bygg en kompakt sammanfattningsrad."""
    dur_min = round((act.get("duration_sec") or 0) / 60)
    avg_hr = act.get("avg_hr")
    load = act.get("training_load")
    dist_m = act.get("distance_m")
    np_watt = act.get("normalized_power")
    avg_power = act.get("avg_power")
    dist_km = round(float(dist_m) / 1000, 1) if dist_m else None
    watts = np_watt or avg_power

    parts = [f"{dur_min} min"]
    if avg_hr:
        parts.append(f"{avg_hr} bpm")
    if sport == "bike" and watts:
        parts.append(f"{watts} W")
    if dist_km:
        parts.append(f"{dist_km} km")
    if load:
        parts.append(f"TSS {round(float(load))}")

    return {
        "summary": "Genomfört: " + " · ".join(parts),
        "name": act.get("activity_name"),
        "activity_type": act.get("activity_type"),
        "duration_min": dur_min,
        "avg_hr": avg_hr,
        "max_hr": act.get("max_hr"),
        "training_load": round(float(load)) if load else None,
        "distance_km": dist_km,
        "normalized_power": np_watt,
        "avg_power": avg_power,
    }


def _compute_status(
    w_date_iso: str,
    sport: str,
    code: str,
    plan_min: float,
    day_activities: list[dict],
    today: date_type,
) -> dict:
    """Plan-vs-actual-status för ett pass. Ren funktion, inga sidoeffekter.

    Varje element i `day_activities` förväntas ha precomputed `_sport` (mappad
    disciplin) och `_dur_min` (float minuter) — se `_fetch_week_activities`.
    """
    try:
        w_date = date_type.fromisoformat(str(w_date_iso)[:10])
    except (ValueError, TypeError):
        return {**_STATUS["planned"], "key": "planned", "actual": None}

    if w_date > today:
        return {**_STATUS["planned"], "key": "planned", "actual": None}
    if w_date == today:
        return {**_STATUS["today"], "key": "today", "actual": None}

    # --- Passerat datum ---
    if sport == "rest":
        if day_activities:
            # Tränade på en planerad vilodag → avvikelse, visa vad som gjordes.
            best = min(day_activities, key=lambda a: a["_dur_min"])
            return {
                **_STATUS["rest_broken"], "key": "rest_broken",
                "actual": _build_actual(best, best.get("_sport") or ""),
            }
        return {**_STATUS["rest_ok"], "key": "rest_ok", "actual": None}

    if not day_activities:
        return {**_STATUS["missed"], "key": "missed", "actual": None}

    # Välj bästa matchande aktivitet: föredra rätt disciplin, sedan närmast tid.
    sport_hits = [a for a in day_activities if _sport_matches(sport, code, a.get("_sport"))]
    pool = sport_hits or day_activities
    best = min(pool, key=lambda a: abs(a["_dur_min"] - (plan_min or 0)))

    sport_ok = _sport_matches(sport, code, best.get("_sport"))
    dur_ok = _within_duration_tolerance(plan_min, best["_dur_min"])
    actual = _build_actual(best, best.get("_sport") or sport)

    if sport_ok and dur_ok:
        return {**_STATUS["done"], "key": "done", "actual": actual}
    return {**_STATUS["deviated"], "key": "deviated", "actual": actual}


def _fetch_week_activities(
    client,
    garmin_athlete_id: str | None,
    strava_user_id: str | None,
    week_monday: date_type,
) -> dict[str, list[dict]]:
    """Källagnostisk aktivitetsläsning för veckan, grupperad på lokalt datum.

    En adept har EN primärkälla: har den ett garmin_athlete_id läses
    garmin_coach.activities, annars Strava (public.strava_activities för
    externa vänner). Båda normaliseras till samma dict-form som
    `_compute_status`/`_build_actual` konsumerar.
    """
    if garmin_athlete_id:
        return _fetch_garmin_week_activities(client, garmin_athlete_id, week_monday)
    if strava_user_id:
        return _fetch_strava_week_activities(client, strava_user_id, week_monday)
    return {}


def _fetch_garmin_week_activities(
    client, garmin_athlete_id: str, week_monday: date_type
) -> dict[str, list[dict]]:
    """Hämta Garmin-aktiviteter för veckan, grupperade på lokalt datum.

    Hämtar ett dygn extra i varje ände (UTC-fönster) och bucketar på lokal
    starttid, så aktiviteter nära midnatt hamnar på rätt kalenderdag.
    """
    win_start = (week_monday - timedelta(days=1)).isoformat()
    win_end = (week_monday + timedelta(days=8)).isoformat()
    try:
        res = (
            client.schema("garmin_coach")
            .table("activities")
            .select(
                "start_time, start_time_local, activity_type, activity_name,"
                " duration_sec, avg_hr, max_hr, training_load, distance_m,"
                " normalized_power, avg_power"
            )
            .eq("athlete_id", garmin_athlete_id)
            .gte("start_time", win_start)
            .lt("start_time", win_end)
            .order("start_time")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return {}

    by_date: dict[str, list[dict]] = {}
    for act in res.data or []:
        day = _activity_local_date(act)
        if not day:
            continue
        act["_sport"] = _ACTIVITY_SPORT_MAP.get(act.get("activity_type"))
        act["_dur_min"] = (act.get("duration_sec") or 0) / 60.0
        by_date.setdefault(day, []).append(act)
    return by_date


def _normalize_strava_activity(row: dict) -> dict:
    """En strava_activities-rad → samma form som garmin-grenen producerar.

    Strava saknar max_hr, training_load (TSS) och normalized_power — de blir
    None och utelämnas då snyggt i actual-raden.
    """
    dur_min = float(row.get("duration_min") or 0)
    dist_km = row.get("distance_km")
    return {
        "_sport": _STRAVA_TYPE_TO_SPORT.get(row.get("type")),
        "_dur_min": dur_min,
        "activity_name": row.get("name"),
        "activity_type": row.get("type"),
        "duration_sec": int(round(dur_min * 60)),
        "avg_hr": row.get("avg_hr"),
        "max_hr": None,
        "training_load": None,
        "distance_m": round(float(dist_km) * 1000) if dist_km else None,
        "normalized_power": None,
        "avg_power": row.get("avg_power"),
        "start_time_local": row.get("date"),  # date-granularitet (lokalt datum)
    }


def _fetch_strava_week_activities(
    client, strava_user_id: str, week_monday: date_type
) -> dict[str, list[dict]]:
    """Hämta Strava-aktiviteter (public.strava_activities) för veckan.

    Strava-raden har bara `date` (lokalt datum, ingen tid), så vi filtrerar
    direkt på kalenderveckan utan UTC-marginal.
    """
    week_start = week_monday.isoformat()
    week_end = (week_monday + timedelta(days=7)).isoformat()
    try:
        res = (
            client.table("strava_activities")
            .select("date, type, name, duration_min, distance_km, avg_hr, avg_power")
            .eq("user_id", strava_user_id)
            .gte("date", week_start)
            .lt("date", week_end)
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return {}

    by_date: dict[str, list[dict]] = {}
    for row in res.data or []:
        day = str(row.get("date"))[:10] if row.get("date") else None
        if not day:
            continue
        by_date.setdefault(day, []).append(_normalize_strava_activity(row))
    return by_date


# Svenska sportnamn i planned_sessions (coach/Nils) → Trixas discipliner.
_PLANNED_SV_SPORT = {
    "Cykel": "bike", "Löpning": "run", "Lopning": "run",
    "Simning": "swim", "Sim": "swim", "Styrka": "strength",
    "Vila": "rest", "Yoga": "rest", "Promenad": "rest", "Vandring": "rest",
}


def _fetch_planned_sessions_week(client, user_id, week_monday):
    """Coachens/Nils plan (public.planned_sessions) för veckan, eller None."""
    if not user_id:
        return None
    start = week_monday.isoformat()
    end = (week_monday + timedelta(days=6)).isoformat()
    try:
        res = (
            client.table("planned_sessions")
            .select("id, date, sport, title, details, purpose, duration_min, steps")
            .eq("user_id", user_id)
            .gte("date", start)
            .lte("date", end)
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    return res.data or None


def _fetch_current_week_data(
    client,
    athlete_id: str,
    year: int,
    week_num: int,
    garmin_athlete_id: str | None = None,
    strava_user_id: str | None = None,
    today: date_type | None = None,
    user_id: str | None = None,
) -> dict | None:
    """Veckans plan + plan-vs-actual.

    Planerad källa: COACHENS plan (planned_sessions) när den finns, annars
    Trixa2:s engine-plan (training_weeks/workouts). Utfört: Garmin/Strava.
    """
    if today is None:
        today = date_type.today()
    week_monday = date_type.fromisocalendar(year, week_num, 1)

    # Utfört (Garmin ELLER Strava) — hoppas över för rena framtidsveckor
    activities_by_date: dict[str, list[dict]] = {}
    if (garmin_athlete_id or strava_user_id) and week_monday <= today:
        activities_by_date = _fetch_week_activities(
            client, garmin_athlete_id, strava_user_id, week_monday
        )

    # Coachens plan har företräde
    coach_sessions = _fetch_planned_sessions_week(client, user_id, week_monday)

    # Engine-plan (training_weeks/workouts) — kan saknas
    phase = None
    week_id = None
    engine_workouts: list[dict] = []
    plan_res = (
        client.table("training_plans").select("id")
        .eq("athlete_id", athlete_id).eq("is_active", True).limit(1).execute()
    )
    if plan_res.data:
        week_res = (
            client.table("training_weeks").select("*")
            .eq("plan_id", plan_res.data[0]["id"])
            .eq("year", year).eq("week_number", week_num).limit(1).execute()
        )
        if week_res.data:
            phase = week_res.data[0].get("phase")
            week_id = week_res.data[0]["id"]
            wres = (
                client.table("workouts").select("*")
                .eq("week_id", week_id).order("date").execute()
            )
            engine_workouts = wres.data or []

    if not coach_sessions and not engine_workouts:
        return None

    week = {
        "id": week_id,
        "week_start": week_monday.isoformat(),
        "week_end": (week_monday + timedelta(days=6)).isoformat(),
        "phase": phase,
        "plan_source": "coach" if coach_sessions else "engine",
        "workouts": [],
    }

    def _status(d, sport, code, dur):
        return _compute_status(
            d, sport, code, dur, activities_by_date.get(str(d)[:10], []), today
        )

    if coach_sessions:
        for ps in coach_sessions:
            sport = _PLANNED_SV_SPORT.get(
                ps.get("sport"), (ps.get("sport") or "").strip().lower()
            )
            title = ps.get("title") or "Pass"
            dur = ps.get("duration_min") or 0
            week["workouts"].append({
                "id": ps.get("id"), "date": ps["date"], "sport": sport,
                "title": title, "code": "", "category": "", "setting": "",
                "duration_minutes": dur, "distance": "",
                "intensity": ps.get("purpose") or "",
                "notes": ps.get("details") or "", "steps": ps.get("steps") or [],
                "coach_notes": "", "is_manual": False,
                "status": _status(ps["date"], sport, title, dur),
            })
    else:
        from coach.engine.loader import load_workouts
        pool = {w["code"]: w for w in load_workouts()}
        for w in engine_workouts:
            code = w.get("title_simple") or w["title"]
            category = code.split("_")[0][:2] if "_" in code else ""
            wd = pool.get(code) or {}
            setting = wd.get("setting") or ("either" if w["sport"] != "rest" else "")
            if wd.get("requires_trainer"):
                setting = "indoor"
            elif wd.get("outdoor_only"):
                setting = "outdoor"
            week["workouts"].append({
                "id": w["id"], "date": w["date"], "sport": w["sport"],
                "title": w["title"], "code": code, "category": category,
                "setting": setting, "duration_minutes": w.get("duration_minutes") or 0,
                "distance": w.get("distance") or "", "intensity": w.get("intensity") or "",
                "notes": w.get("notes") or "", "steps": w.get("steps") or [],
                "coach_notes": w.get("coach_notes") or "",
                "is_manual": w.get("is_manual", False),
                "status": _status(w["date"], w["sport"], code, w.get("duration_minutes") or 0),
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
def settings_view(request: Request, saved: bool = False, strava: str = "") -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select(
            "sports, long_bike_day, long_run_day, preferred_rest_days,"
            " equipment, preferred_settings, use_strava, garmin_athlete_id"
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

    # Strava-anslutningsstatus
    tok = client.table("strava_tokens").select("athlete_id").eq("user_id", user_id).limit(1).execute()
    last = (
        client.table("strava_activities").select("date")
        .eq("user_id", user_id).order("date", desc=True).limit(1).execute()
    )
    strava_status = {
        "connected": bool(tok.data),
        "athlete_id": tok.data[0]["athlete_id"] if tok.data else None,
        "use_strava": athlete.get("use_strava", False),
        "has_garmin": bool(athlete.get("garmin_athlete_id")),
        "last_activity": last.data[0]["date"] if last.data else None,
        "configured": strava_client.creds_configured(),
        "flash": strava,
    }
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
            "strava": strava_status,
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


# ---------- Debug: vad Trixa ser ----------


@router.get("/debug", response_class=HTMLResponse)
def debug_view(request: Request) -> HTMLResponse:
    """Transparens-vy: alla datakällor + engine-beslut för aktuell vecka."""
    user_id = _current_user_id(request)
    week_start = _monday_of(date_type.today())

    try:
        plan = generate_week(
            athlete_user_id=user_id,
            week_start=week_start,
            dry_run=True,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    # Hämta concerns separat (visas i en egen tabell)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select("active_concerns")
        .eq("user_id", user_id)
        .execute()
    )
    concerns = a_res.data[0].get("active_concerns") or [] if a_res.data else []

    ds = plan.engine_decisions.get("_data_sources", {})
    ot = ds.get("ot_signals", {})

    # Gap-procent (faktisk / deklarerat)
    actual = ds.get("actual_weekly_hours_4w_avg")
    declared = ds.get("declared_weekly_hours") or 0
    gap_pct = round(actual / declared * 100) if (actual and declared > 0) else None

    return _render(
        "debug.html",
        {
            "request": request,
            "plan": plan,
            "ds": ds,
            "ot": ot,
            "gap_pct": gap_pct,
            "concerns": concerns,
            "data_warnings": plan.engine_decisions.get("_warnings", []),
            "raw_json": json.dumps(
                plan.to_dict(), indent=2, ensure_ascii=False, default=str
            ),
        },
    )


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


@router.post("/workouts/custom", response_class=HTMLResponse)
def workout_add_custom(
    request: Request,
    week_id: str = Form(...),
    date: str = Form(...),
    sport: str = Form(...),
    distance: str = Form(""),
    duration_minutes: int | None = Form(None),
    description: str = Form(""),
) -> Any:
    """Lägg till ett adept-skapat 'eget pass' på en dag. Markeras is_manual=true
    så det överlever regenerering av veckan, och matchas i plan-vs-actual."""
    if sport not in ("swim", "bike", "run", "strength"):
        raise HTTPException(400, "Gren måste vara swim, bike, run eller strength")
    client = get_postgrest()
    wk = client.table("training_weeks").select("plan_id").eq("id", week_id).limit(1).execute()
    if not wk.data:
        raise HTTPException(404, "Vecka saknas")
    pl = (
        client.table("training_plans")
        .select("athlete_id")
        .eq("id", wk.data[0]["plan_id"])
        .limit(1)
        .execute()
    )
    if not pl.data:
        raise HTTPException(404, "Plan saknas")
    title = (description or "").strip() or "Eget pass"
    client.table("workouts").insert({
        "week_id": week_id,
        "athlete_id": pl.data[0]["athlete_id"],
        "date": date,
        "sport": sport,
        "title": title,
        "duration_minutes": duration_minutes,
        "distance": (distance or "").strip() or None,
        "notes": (description or "").strip() or None,
        "is_manual": True,
    }).execute()
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/workouts/{workout_id}/delete-custom", response_class=HTMLResponse)
def workout_delete_custom(request: Request, workout_id: str) -> Any:
    """Ta bort ett eget pass. Begränsat till is_manual=true — genererade pass
    kan inte raderas den här vägen."""
    client = get_postgrest()
    client.table("workouts").delete().eq("id", workout_id).eq("is_manual", True).execute()
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
