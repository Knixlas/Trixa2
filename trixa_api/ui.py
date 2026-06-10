"""HTML-formulär och vyer för Trixa.

Tunt skal: använder samma logik som API:t men returnerar Jinja-renderad HTML.
Auth: separat — för adept-UI används samma Bearer-token i cookie (MVP).
För riktig deploy: byt till Supabase JWT med signing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type, timedelta
from pathlib import Path

import requests

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from coach.trixa.db import get_postgrest
from coach.trixa.planner import (
    _resolve_activity_sources,
    generate_week,
)
from trixa_api import season, supabase_auth, readiness, strava_client


logger = logging.getLogger("trixa.ui")

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
        # Koppling gör INTE Strava till aktiv källa: Garmin förblir primär och
        # Strava blir reserv (glappfyllare). Vill adepten tvinga Strava finns
        # nödutgången /strava/use. Garmin-lösa adepter läser Strava ändå.
        strava_client.sync_recent(client, uid, days=45)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/ui/settings?strava=error", status_code=303)
    return RedirectResponse("/ui/settings?strava=connected", status_code=303)


@router.post("/strava/sync")
def strava_sync(request: Request) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    if not strava_client.creds_configured():
        # Utan STRAVA_CLIENT_ID/SECRET kan token-förnyelsen aldrig lyckas —
        # säg det rakt ut istället för generiskt "något gick fel".
        return RedirectResponse("/ui/settings?strava=noconfig", status_code=303)
    try:
        strava_client.sync_recent(client, uid, days=45)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        body = (exc.response.text or "")[:300] if exc.response is not None else ""
        logger.error("Strava-sync HTTP %s för %s: %s", code, uid, body)
        return RedirectResponse(f"/ui/settings?strava=error&why=http{code}", status_code=303)
    except Exception:  # noqa: BLE001
        logger.exception("Strava-sync kraschade för %s", uid)
        return RedirectResponse("/ui/settings?strava=error", status_code=303)
    return RedirectResponse("/ui/settings?strava=synced", status_code=303)


@router.post("/strava/disconnect")
def strava_disconnect(request: Request) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    strava_client.delete_tokens(client, uid)
    client.table("athlete_profiles").update({"use_strava": False}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=disconnected", status_code=303)


@router.post("/strava/use")
def strava_use(request: Request) -> Any:
    """Manuell nödutgång: tvinga Strava som källa (Garmin ignoreras).

    För perioder då Garmin-synken ligger nere. Återställs med /strava/auto.
    """
    uid = _current_user_id(request)
    client = get_postgrest()
    client.table("athlete_profiles").update({"use_strava": True}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=using", status_code=303)


@router.post("/strava/auto")
def strava_auto(request: Request) -> Any:
    """Återgå till Garmin-primär (Strava som reserv/glappfyllare)."""
    uid = _current_user_id(request)
    client = get_postgrest()
    client.table("athlete_profiles").update({"use_strava": False}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=auto", status_code=303)


# ---------- Styrkelogg (set/reps/vikt/ansträngning mot styrkepassen) ----------


@router.post("/strength/log")
def strength_log(
    request: Request,
    session_date: str = Form(...),
    exercise_name: str = Form(...),
    sets: int | None = Form(None),
    reps: int | None = Form(None),
    weight_from: float | None = Form(None),
    effort: int = Form(2),
) -> Any:
    """Logga en utförd styrkeövning. Upsert på (user, datum, övningsnamn)."""
    uid = _current_user_id(request)
    name = (exercise_name or "").strip()
    if not uid or not name or not session_date:
        raise HTTPException(400, "exercise_name och session_date krävs")
    if effort not in (-1, 1, 2, 3, 4):
        effort = 2
    client = get_postgrest()
    row = {
        "user_id": uid, "session_date": session_date, "exercise_name": name,
        "sets": sets, "reps": reps, "weight_from": weight_from, "effort": effort,
    }
    existing = (
        client.table("exercise_logs").select("id")
        .eq("user_id", uid).eq("session_date", session_date)
        .eq("exercise_name", name).execute()
    )
    if existing.data:
        client.table("exercise_logs").update(row).eq("id", existing.data[0]["id"]).execute()
    else:
        client.table("exercise_logs").insert(row).execute()
    return RedirectResponse("/ui/", status_code=303)


@router.post("/strength/remove")
def strength_remove(request: Request, log_id: str = Form(...)) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    client.table("exercise_logs").delete().eq("id", log_id).eq("user_id", uid).execute()
    return RedirectResponse("/ui/", status_code=303)


def _monday_of(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


# ---------- Dashboard ----------


# ---------- Säsongs-tidslinje (fas-staplar + följsamhet bakåt) ----------

# Vecko-cellernas följsamhetsfärger (mättare än fas-staplarna, så raden läses
# som en egen "hur gick det"-axel).
_COMPLIANCE_CELL = {"green": "#34d399", "yellow": "#fbbf24", "red": "#f87171"}
_COMPLIANCE_LABEL = {
    "green": "bra följsamhet", "yellow": "ok följsamhet", "red": "låg följsamhet",
}


# Tidslinjens följsamhet behöver inte obegränsad historik.
_COMPLIANCE_MAX_WEEKS = 12


def _fetch_activities_range(
    client, garmin_id, strava_user_id, start: date_type, end: date_type
) -> dict[str, list[dict]]:
    """Aktiviteter över ett datumspann, grupperade på lokalt datum.

    EN query mot Garmin/TP-cachen + EN mot Strava (gap-fill per dag) — istället
    för ett anrop per vecka. Samma normalisering som veckoläsarna.
    """
    by_date: dict[str, list[dict]] = {}
    if garmin_id:
        try:
            res = (
                client.schema("garmin_coach")
                .table("activities")
                .select(
                    "start_time, start_time_local, activity_type, activity_name,"
                    " duration_sec, avg_hr, max_hr, training_load, distance_m,"
                    " normalized_power, avg_power"
                )
                .eq("athlete_id", garmin_id)
                .gte("start_time", (start - timedelta(days=1)).isoformat())
                .lt("start_time", (end + timedelta(days=2)).isoformat())
                .order("start_time")
                .execute()
            )
            for act in res.data or []:
                day = _activity_local_date(act)
                if not day:
                    continue
                act["_sport"] = _ACTIVITY_SPORT_MAP.get(act.get("activity_type"))
                act["_dur_min"] = (act.get("duration_sec") or 0) / 60.0
                by_date.setdefault(day, []).append(act)
        except Exception:  # noqa: BLE001
            pass
    if strava_user_id:
        try:
            res = (
                client.table("strava_activities")
                .select("date, type, name, duration_min, distance_km, avg_hr, avg_power")
                .eq("user_id", strava_user_id)
                .gte("date", start.isoformat())
                .lte("date", end.isoformat())
                .order("date")
                .execute()
            )
            for row in res.data or []:
                day = str(row.get("date"))[:10]
                if not day or day in by_date:
                    continue  # Garmin/TP har dagen → den datan vinner
                by_date.setdefault(day, []).append(_normalize_strava_activity(row))
        except Exception:  # noqa: BLE001
            pass
    return by_date


def _compliance_by_week(client, athlete_id, garmin_id, strava_user_id, today, user_id) -> dict:
    """Följsamhets-bucket per planerad, passerad vecka. Nyckel: (iso_year, iso_week).

    Bulk-läsning: EN query för planerade pass + EN-TVÅ för aktiviteter över hela
    fönstret. Tidigare gjordes en serie anrop PER VECKA (planned_sessions +
    aktiviteter + styrkeloggar) — med veckor sedan mars blev det ~50 sekventiella
    DB-roundtrips per dashboard-laddning, därav segheten.
    """
    if not user_id:
        return {}
    this_monday = today - timedelta(days=today.weekday())
    window_start = this_monday - timedelta(weeks=_COMPLIANCE_MAX_WEEKS)

    try:
        ps_res = (
            client.table("planned_sessions")
            .select("date, sport, title, workout_code, duration_min")
            .eq("user_id", user_id)
            .gte("date", window_start.isoformat())
            .lte("date", today.isoformat())
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return {}
    sessions = ps_res.data or []
    if not sessions:
        return {}

    activities_by_date = _fetch_activities_range(
        client, garmin_id, strava_user_id, window_start, today
    )

    weeks: dict[tuple[int, int], list[dict]] = {}
    for ps in sessions:
        try:
            d = date_type.fromisoformat(str(ps.get("date"))[:10])
        except (ValueError, TypeError):
            continue
        sport = _PLANNED_SV_SPORT.get(
            ps.get("sport"), (ps.get("sport") or "").strip().lower()
        )
        code = ps.get("workout_code") or ""
        status = _compute_status(
            ps["date"], sport, code or (ps.get("title") or ""),
            ps.get("duration_min") or 0,
            activities_by_date.get(str(ps["date"])[:10], []), today,
        )
        iso = d.isocalendar()
        weeks.setdefault((iso[0], iso[1]), []).append({"status": status})

    out: dict = {}
    for (y, wn), workouts in weeks.items():
        if date_type.fromisocalendar(y, wn, 1) > today:
            continue
        bucket = season.compliance_bucket(workouts, today)
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

    garmin_id, strava_user_id = _resolve_activity_sources(athlete)

    try:
        comp_map = _compliance_by_week(
            client, athlete["id"], garmin_id, strava_user_id,
            today, athlete.get("user_id"),
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

    # Garmin primär, Strava reserv/nödutgång (se _resolve_activity_sources).
    uid = athlete.get("user_id")
    garmin_id, strava_user_id = _resolve_activity_sources(athlete)
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
        # Visa även dagens utförda pass (om något loggats) — behåll "Idag"-badgen.
        actual = None
        if sport != "rest" and day_activities:
            sport_hits = [a for a in day_activities if _sport_matches(sport, code, a.get("_sport"))]
            pool = sport_hits or day_activities
            best = min(pool, key=lambda a: abs(a["_dur_min"] - (plan_min or 0)))
            actual = _build_actual(best, best.get("_sport") or sport)
        return {**_STATUS["today"], "key": "today", "actual": actual}

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
    """Källprioriterad aktivitetsläsning för veckan, grupperad på lokalt datum.

    Garmin är primär: finns ett garmin_athlete_id läses garmin_coach.activities
    och DEN datan litar vi på. Bara om veckan saknar Garmin-pass (sync-glapp)
    faller vi tillbaka på Strava för just den veckan. Garmin-lösa adepter läser
    Strava direkt. Båda normaliseras till samma dict-form som
    `_compute_status`/`_build_actual` konsumerar.
    """
    if garmin_athlete_id:
        by_date = _fetch_garmin_week_activities(client, garmin_athlete_id, week_monday)
        if by_date or not strava_user_id:
            return by_date
        # Garmin-glapp denna vecka → reserv från Strava (rör Strava bara här).
        return _fetch_strava_week_activities(client, strava_user_id, week_monday)
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
            .select(
                "id, date, sport, title, details, purpose, duration_min,"
                " steps, exercises, origin, workout_code, intensity"
            )
            .eq("user_id", user_id)
            .gte("date", start)
            .lte("date", end)
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    return res.data or None


def _attach_strength_logs(client, week: dict, user_id: str) -> None:
    """Lägg loggade styrkeövningar (exercise_logs) på styrkepassen i veckan,
    plus en datalist med adeptens tidigare övningsnamn (för snabb inmatning)."""
    if not user_id:
        return
    strength = [w for w in week["workouts"] if w["sport"] == "strength"]
    if not strength:
        return
    try:
        logs = (
            client.table("exercise_logs")
            .select("id, session_date, exercise_name, sets, reps, weight_from, effort")
            .eq("user_id", user_id)
            .gte("session_date", week["week_start"])
            .lte("session_date", week["week_end"])
            .execute()
        )
        prev = (
            client.table("exercise_logs").select("exercise_name")
            .eq("user_id", user_id).limit(500).execute()
        )
    except Exception:  # noqa: BLE001
        return
    by_date: dict[str, list[dict]] = {}
    for lg in logs.data or []:
        by_date.setdefault(str(lg.get("session_date"))[:10], []).append(lg)
    week["exercise_suggestions"] = sorted({
        (r.get("exercise_name") or "").strip()
        for r in (prev.data or []) if (r.get("exercise_name") or "").strip()
    })
    for w in strength:
        w["logged_exercises"] = by_date.get(str(w["date"])[:10], [])


def _display_steps(steps) -> list[dict]:
    """Normalisera pass-steg för rendering.

    Steps i planned_sessions kan bära passbankens template-form där t.ex.
    `sets` är en dict ({"range": [4,10], "default": 6}) istället för ett tal.
    Templaten jämför `s.sets > 1` — en dict där kraschar Jinja med TypeError
    (orsakade 500 på dashboarden). Plocka default-värdet för visning.
    """
    def _scalar(v):
        if isinstance(v, dict):
            v = v.get("default") or v.get("estimated") or (v.get("range") or [None])[0]
        return v if isinstance(v, (int, float, str)) or v is None else None

    out: list[dict] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        c = dict(s)
        for k in ("sets", "distance_m", "duration_min", "rest_sec", "zone"):
            if k in c:
                c[k] = _scalar(c[k])
        out.append(c)
    return out


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

    # MASTER: planen läses från planned_sessions (docs/08). Raderna kan komma
    # från Nils (origin='nils'), motorn (origin='trixa2') eller legacy (NULL).
    sessions = _fetch_planned_sessions_week(client, user_id, week_monday)
    if not sessions:
        return None

    # Veckans källmärkning: finns en enda Nils-rad är veckan coach-styrd —
    # då gäller hens plan och regenerering ska inte erbjudas.
    has_coach_rows = any((ps.get("origin") or "") == "nils" for ps in sessions)

    week = {
        "id": None,
        "week_start": week_monday.isoformat(),
        "week_end": (week_monday + timedelta(days=6)).isoformat(),
        "phase": None,
        "plan_source": "coach" if has_coach_rows else "engine",
        "workouts": [],
    }

    def _status(d, sport, code, dur):
        return _compute_status(
            d, sport, code, dur, activities_by_date.get(str(d)[:10], []), today
        )

    for ps in sessions:
        sport = _PLANNED_SV_SPORT.get(
            ps.get("sport"), (ps.get("sport") or "").strip().lower()
        )
        title = ps.get("title") or "Pass"
        dur = ps.get("duration_min") or 0
        code = ps.get("workout_code") or ""
        week["workouts"].append({
            "id": ps.get("id"), "date": ps["date"], "sport": sport,
            "title": title, "code": code, "category": "", "setting": "",
            "duration_minutes": dur, "distance": "",
            "intensity": ps.get("intensity") or ps.get("purpose") or "",
            "notes": ps.get("details") or "", "steps": _display_steps(ps.get("steps")),
            "coach_notes": "",
            "is_manual": (ps.get("origin") or "") == "manual",
            "origin": ps.get("origin") or "",
            "planned_exercises": ps.get("exercises") or [],
            "status": _status(ps["date"], sport, code or title, dur),
        })

    _attach_strength_logs(client, week, user_id)
    return week


# ---------- Plan-preview ----------


@router.get("/plan")
def plan_view(request: Request) -> Any:
    """Hemmet visar redan både denna och nästa vecka — den gamla separata
    "Veckans plan"-vyn (som förvirrande nog visade NÄSTA veckas dry-run)
    är borttagen. Gamla bokmärken landar på hemskärmen."""
    return RedirectResponse(url="/ui/", status_code=303)


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
def settings_view(
    request: Request, saved: bool = False, strava: str = "", why: str = ""
) -> HTMLResponse:
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
        "why": why,
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


# ---------- Nyckeltal (datasida: träning + hälsa) ----------

# training_log.sport är blandad vokabulär (svenska + engelska, gamla + nya
# skrivare). Normalisera till intern disciplin för aggregering.
_TL_SPORT = {
    "lopning": "run", "löpning": "run", "run": "run", "trailrun": "run",
    "cykel": "bike", "bike": "bike", "ride": "bike", "cykling": "bike",
    "sim": "swim", "swim": "swim", "simning": "swim",
    "styrka": "strength", "strength": "strength", "weighttraining": "strength",
}
_SPORT_LABEL = {
    "swim": "Simning", "bike": "Cykel", "run": "Löpning",
    "strength": "Styrka", "other": "Övrigt",
}


def _fnum(v) -> float | None:
    """daily_metrics levererar numerics som strängar — tolerant float."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _latest_value(metrics: list[dict], key: str):
    """Senaste icke-null-värde (listan är nyast först)."""
    for row in metrics:
        if row.get(key) is not None:
            return row[key], str(row.get("metric_date"))[:10]
    return None, None


def _avg7(metrics: list[dict], key: str) -> float | None:
    vals = [_fnum(r.get(key)) for r in metrics[:7]]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _load_zone(ratio: float | None) -> dict | None:
    """ACWR-zon: under 0.8 = lugnt, 0.8-1.3 = lagom, över 1.3 = hög risk."""
    if ratio is None:
        return None
    if ratio > 1.3:
        return {"label": "Hög — över skadezonen", "color": "var(--coral)"}
    if ratio >= 0.8:
        return {"label": "Lagom belastningsökning", "color": "var(--palm)"}
    return {"label": "Lugn — utrymme att bygga", "color": "var(--lagoon)"}


def _hrv_status(latest: float | None, low: float | None, high: float | None) -> dict | None:
    if latest is None or low is None:
        return None
    if latest < low:
        return {"label": "Under din baseline — ta det lugnt", "color": "var(--coral)"}
    if high is not None and latest > high:
        return {"label": "Över baseline — välåterhämtad", "color": "var(--lagoon)"}
    return {"label": "Inom din baseline", "color": "var(--palm)"}


def _build_data_context(client, athlete: dict, today: date_type) -> dict:
    """Nyckeltal för datasidan. Bulk: 1 query daily_metrics + 1 training_log."""
    user_id = athlete.get("user_id")
    this_monday = _monday_of(today)

    # --- Hälsa: senaste 14 d ur daily_metrics (nyast först) ---
    metrics: list[dict] = []
    if athlete.get("garmin_athlete_id"):
        try:
            res = (
                client.schema("garmin_coach")
                .table("daily_metrics")
                .select(
                    "metric_date, resting_hr, hrv_last_night_ms, hrv_baseline_low,"
                    " hrv_baseline_high, sleep_score, readiness_score, stress_avg,"
                    " acute_load, chronic_load, load_ratio"
                )
                .eq("athlete_id", athlete["garmin_athlete_id"])
                .gte("metric_date", (today - timedelta(days=14)).isoformat())
                .order("metric_date", desc=True)
                .execute()
            )
            metrics = res.data or []
        except Exception:  # noqa: BLE001
            metrics = []

    rhr, _ = _latest_value(metrics, "resting_hr")
    hrv_raw, _ = _latest_value(metrics, "hrv_last_night_ms")
    hrv = _fnum(hrv_raw)
    hrv_low = _fnum(metrics[0].get("hrv_baseline_low")) if metrics else None
    hrv_high = _fnum(metrics[0].get("hrv_baseline_high")) if metrics else None
    sleep, _ = _latest_value(metrics, "sleep_score")
    ratio_raw, _ = _latest_value(metrics, "load_ratio")
    ratio = _fnum(ratio_raw)
    acute, _ = _latest_value(metrics, "acute_load")
    chronic, _ = _latest_value(metrics, "chronic_load")
    metric_date = str(metrics[0].get("metric_date"))[:10] if metrics else None
    stale_days = (today - date_type.fromisoformat(metric_date)).days if metric_date else None

    health = {
        "metric_date": metric_date,
        "stale": stale_days is not None and stale_days > 1,
        "stale_days": stale_days,
        "rhr": rhr,
        "rhr_avg7": _avg7(metrics, "resting_hr"),
        "hrv": round(hrv) if hrv is not None else None,
        "hrv_low": hrv_low,
        "hrv_high": hrv_high,
        "hrv_status": _hrv_status(hrv, hrv_low, hrv_high),
        "sleep": sleep,
        "sleep_avg7": _avg7(metrics, "sleep_score"),
        "load_ratio": round(ratio, 2) if ratio is not None else None,
        "load_zone": _load_zone(ratio),
        "acute": round(_fnum(acute)) if _fnum(acute) is not None else None,
        "chronic": round(_fnum(chronic)) if _fnum(chronic) is not None else None,
    }

    # --- Träning: senaste 6 ISO-veckor (inkl. innevarande) ur MASTER training_log ---
    window_start = this_monday - timedelta(weeks=5)
    rows: list[dict] = []
    try:
        res = (
            client.table("training_log")
            .select("date, sport, duration_min, distance_km, tss")
            .eq("user_id", user_id)
            .gte("date", window_start.isoformat())
            .lte("date", today.isoformat())
            .order("date")
            .execute()
        )
        rows = res.data or []
    except Exception:  # noqa: BLE001
        rows = []

    week_hours: dict[date_type, float] = {}
    week_tss: dict[date_type, float] = {}
    week_count: dict[date_type, int] = {}
    this_week_disc: dict[str, float] = {}
    disc_4w_hours: dict[str, float] = {}
    disc_4w_dist: dict[str, float] = {}
    four_w_start = this_monday - timedelta(weeks=4)
    for r in rows:
        try:
            d = date_type.fromisoformat(str(r.get("date"))[:10])
        except (ValueError, TypeError):
            continue
        monday = _monday_of(d)
        h = (_fnum(r.get("duration_min")) or 0.0) / 60.0
        week_hours[monday] = week_hours.get(monday, 0.0) + h
        week_tss[monday] = week_tss.get(monday, 0.0) + (_fnum(r.get("tss")) or 0.0)
        week_count[monday] = week_count.get(monday, 0) + 1
        disc = _TL_SPORT.get((r.get("sport") or "").strip().lower(), "other")
        if monday == this_monday:
            this_week_disc[disc] = this_week_disc.get(disc, 0.0) + h
        if four_w_start <= d < this_monday:
            disc_4w_hours[disc] = disc_4w_hours.get(disc, 0.0) + h
            disc_4w_dist[disc] = disc_4w_dist.get(disc, 0.0) + (_fnum(r.get("distance_km")) or 0.0)

    series = []
    max_h = 0.0
    for i in range(5, -1, -1):
        monday = this_monday - timedelta(weeks=i)
        h = round(week_hours.get(monday, 0.0), 1)
        max_h = max(max_h, h)
        series.append({
            "label": f"v{monday.isocalendar()[1]}",
            "hours": h,
            "current": monday == this_monday,
        })
    for s in series:
        s["pct"] = round(s["hours"] / max_h * 100) if max_h > 0 else 0

    completed = [week_hours.get(this_monday - timedelta(weeks=i), 0.0) for i in range(1, 5)]
    avg4 = round(sum(completed) / 4, 1)

    training = {
        "this_week_hours": round(week_hours.get(this_monday, 0.0), 1),
        "this_week_count": week_count.get(this_monday, 0),
        "this_week_tss": round(week_tss.get(this_monday, 0.0)),
        "this_week_disc": [
            {"label": _SPORT_LABEL.get(k, k), "hours": round(v, 1)}
            for k, v in sorted(this_week_disc.items(), key=lambda x: -x[1])
        ],
        "avg4_hours": avg4,
        "goal_hours": _fnum(athlete.get("weekly_hours")),
        "series": series,
        "disc_4w": [
            {
                "label": _SPORT_LABEL.get(k, k),
                "hours": round(v, 1),
                "dist_km": round(disc_4w_dist.get(k, 0.0)),
            }
            for k, v in sorted(disc_4w_hours.items(), key=lambda x: -x[1])
        ],
    }

    # --- Tävling + testvärden ---
    days_to_race = None
    if athlete.get("race_date"):
        try:
            rd = date_type.fromisoformat(str(athlete["race_date"])[:10])
            days_to_race = (rd - today).days
        except (ValueError, TypeError):
            pass

    profile = {
        "days_to_race": days_to_race,
        "race_date": athlete.get("race_date"),
        "race_type": athlete.get("race_type"),
        "time_goal": athlete.get("time_goal"),
        "ftp": athlete.get("ftp"),
        "lthr": athlete.get("lthr"),
        "swim_css": athlete.get("swim_css"),
        "run_threshold_pace": athlete.get("run_threshold_pace"),
    }

    return {"health": health, "training": training, "profile": profile}


@router.get("/data", response_class=HTMLResponse)
def data_view(request: Request) -> HTMLResponse:
    """Nyckeltal: träning + hälsa på en sida. Deterministisk läsning, bulk-queries."""
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select(
            "user_id, garmin_athlete_id, weekly_hours, race_date, race_type,"
            " time_goal, ftp, lthr, swim_css, run_threshold_pace"
        )
        .eq("user_id", user_id)
        .execute()
    )
    if not a_res.data:
        raise HTTPException(404, "Athlete saknas")
    ctx = _build_data_context(client, a_res.data[0], date_type.today())
    ctx["request"] = request
    return _render("data.html", ctx)


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


# "Byt pass"/"byt gren"-endpoints togs bort 2026-06-10: de skrev mot den
# pensionerade workouts-tabellen och kraschade. Byggs om mot planned_sessions
# när byt-pass-funktionen behövs igen.


@router.post("/workouts/custom", response_class=HTMLResponse)
def workout_add_custom(
    request: Request,
    week_id: str = Form(""),   # legacy, ignoreras (passet kopplas via user_id+date)
    date: str = Form(...),
    sport: str = Form(...),
    distance: str = Form(""),
    duration_minutes: int | None = Form(None),
    description: str = Form(""),
) -> Any:
    """Lägg till ett eget pass i MASTER planned_sessions (origin='manual')."""
    if sport not in ("swim", "bike", "run", "strength"):
        raise HTTPException(400, "Gren måste vara swim, bike, run eller strength")
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Inte inloggad")
    sv_sport = {"swim": "Sim", "bike": "Cykel", "run": "Löpning",
                "strength": "Styrka"}.get(sport, sport)
    client = get_postgrest()
    client.table("planned_sessions").insert({
        "user_id": user_id,
        "date": date,
        "sport": sv_sport,
        "title": (description or "").strip() or "Eget pass",
        "duration_min": duration_minutes,
        "details": (description or "").strip() or None,
        "status": "planned",
        "origin": "manual",
    }).execute()
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/workouts/{workout_id}/delete-custom", response_class=HTMLResponse)
def workout_delete_custom(request: Request, workout_id: str) -> Any:
    """Ta bort ett eget pass i MASTER planned_sessions. Endast origin='manual'."""
    client = get_postgrest()
    client.table("planned_sessions").delete().eq("id", workout_id).eq("origin", "manual").execute()
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
