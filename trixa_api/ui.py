"""HTML-formulär och vyer för Trixa.

Tunt skal: använder samma logik som API:t men returnerar Jinja-renderad HTML.
Auth: separat — för adept-UI används samma Bearer-token i cookie (MVP).
För riktig deploy: byt till Supabase JWT med signing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path

import requests

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from coach.trixa.db import get_postgrest
from coach.trixa.planner import (
    generate_week,
    swap_workout_discipline_and_replan,
    swap_workout_to_next_alternative,
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


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


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


def _ensure_athlete_profile(client, user_id: str, name: str | None = None) -> dict:
    """Se till att användaren har en athlete_profiles-rad; skapa med defaults annars.

    Idempotent (upsert on_conflict user_id) — anropas efter signup OCH som
    säkerhetsnät vid dashboard-access, så användare skapade utanför UI:t
    (admin-API, äldre signups) också får en rad.
    """
    res = (
        client.table("athlete_profiles")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]

    # goal och experience_level är NOT NULL med CHECK-constraint i DB och frågas
    # inte i onboardingen — sätt giltiga platshållare (ändras i Inställningar).
    # goal ∈ first_race|pr|ironman|health|comeback, experience_level ∈ _EXPERIENCE_LEVELS.
    defaults = {
        "user_id": user_id,
        "goal": "ironman",
        "experience_level": "intermediate",
        "sports": ["swim", "bike", "run"],
        "weekly_hours": 6,
        "preferred_rest_days": ["monday"],
        "recovery_week_ratio": "3:1",
        # onboarded_at lämnas NULL → dashboarden redirectar till /ui/onboarding
    }
    client.table("athlete_profiles").upsert(
        defaults, on_conflict="user_id", ignore_duplicates=True
    ).execute()
    res = (
        client.table("athlete_profiles")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        # Tyst retur av defaults (utan "id") gav KeyError längre ned i kedjan —
        # bättre att felet syns här än som mystisk 500 i onboardingen.
        raise RuntimeError(f"Kunde inte skapa athlete_profiles-rad för {user_id}")
    return res.data[0]


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
    # Skapa athlete_profiles-raden direkt — halv-registrerade användare
    # (profiles utan athlete_profiles) kraschar planner och dashboard.
    try:
        _ensure_athlete_profile(get_postgrest(), session["user_id"], name.strip() or None)
    except Exception:  # noqa: BLE001 — säkerhetsnätet i dashboard tar det annars
        logger.exception("Kunde inte skapa athlete_profiles-rad vid signup")
    resp = RedirectResponse(url="/ui/onboarding", status_code=303)
    set_session_cookies(resp, session, secure=is_secure_request(request))
    return resp


# ---------- Strava-koppling (nödreserv för utförd veckovolym) ----------


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
        # Koppling gör INTE Strava till primärkälla: TP/master förblir primär.
        # Strava är bara en grov reserv om training_log saknar utförd volym.
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


@router.post("/tp/sync")
def tp_sync_now(request: Request) -> Any:
    """Hämta nya pass + recovery från TrainingPeaks DIREKT (utan att vänta på
    workerns schemalagda sync). För 'analysera/omplanera direkt efter passet'."""
    uid = _current_user_id(request)
    client = get_postgrest()
    try:
        from coach.integrations.trainingpeaks.auth_store import supabase_cookie_provider
        from coach.integrations.trainingpeaks.run_sync import main as tp_sync_main

        if supabase_cookie_provider(uid, client)() is None:
            return RedirectResponse("/ui/settings?tp=nocookie", status_code=303)
        a = (
            client.table("athlete_profiles").select("garmin_athlete_id")
            .eq("user_id", uid).execute()
        )
        garmin_id = a.data[0].get("garmin_athlete_id") if a.data else None
        if not garmin_id:
            return RedirectResponse("/ui/settings?tp=noprofile", status_code=303)
        # Brett fönster (14 d): manuell knapp = "hämta ikapp", inte bara senaste
        # dygnet. Fångar pass som låg före att master-skrivningen wire:ades in.
        rc = tp_sync_main(["--user", uid, "--athlete-id", str(garmin_id), "--days", "14"])
        flash = "synced" if rc == 0 else "error"
        return RedirectResponse(f"/ui/settings?tp={flash}", status_code=303)
    except Exception:  # noqa: BLE001
        logger.exception("TP-sync via knapp kraschade för %s", uid)
        return RedirectResponse("/ui/settings?tp=error", status_code=303)


@router.post("/tp/login")
def tp_login(
    request: Request,
    tp_username: str = Form(...),
    tp_password: str = Form(...),
) -> Any:
    """Koppla TrainingPeaks med användarnamn + lösenord (headless login).

    Loggar in serverside, sparar BARA den resulterande session-cookien per
    user_id (tp_auth). Lösenordet lagras aldrig — det lämnar aldrig den här
    request-hanteraren.
    """
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    client = get_postgrest()
    try:
        from coach.integrations.trainingpeaks.client import TPAuthError
        from coach.integrations.trainingpeaks.login import login_and_store

        login_and_store(tp_username, tp_password, uid, pg=client)
    except TPAuthError:
        # Fel uppgifter eller CAPTCHA/MFA — vägled (utan att läcka detalj).
        return RedirectResponse("/ui/settings?tp=loginfail", status_code=303)
    except Exception:  # noqa: BLE001
        logger.exception("TP-login via knapp kraschade för %s", uid)
        return RedirectResponse("/ui/settings?tp=loginerror", status_code=303)
    return RedirectResponse("/ui/settings?tp=connected", status_code=303)


@router.post("/strava/disconnect")
def strava_disconnect(request: Request) -> Any:
    uid = _current_user_id(request)
    client = get_postgrest()
    strava_client.delete_tokens(client, uid)
    client.table("athlete_profiles").update({"use_strava": False}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=disconnected", status_code=303)


@router.post("/strava/use")
def strava_use(request: Request) -> Any:
    """Manuell nödutgång: använd Strava som reserv för veckovolym.

    TP-matad recovery används fortfarande när den finns. Återställs med
    /strava/auto.
    """
    uid = _current_user_id(request)
    client = get_postgrest()
    client.table("athlete_profiles").update({"use_strava": True}).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?strava=using", status_code=303)


@router.post("/strava/auto")
def strava_auto(request: Request) -> Any:
    """Återgå till TP/master utan Strava-reserv."""
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


@router.post("/settings/connections")
def settings_connections(
    request: Request,
    conn_ai: str | None = Form(None),
    conn_tp: str | None = Form(None),
    conn_strava: str | None = Form(None),
) -> Any:
    """Spara bara kapabilitets-flaggorna (egen form → nollställer inte övrigt)."""
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    client = get_postgrest()
    client.table("athlete_profiles").update({
        "conn_ai": conn_ai == "1",
        "conn_tp": conn_tp == "1",
        "conn_strava": conn_strava == "1",
    }).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?saved=1", status_code=303)


@router.post("/api-tokens", response_class=HTMLResponse)
def api_token_create(request: Request, name: str = Form("AI-token")) -> Any:
    """Skapa en per-adept API-token för extern AI (Nils). Råvärdet visas EN
    gång (renderas direkt, hamnar aldrig i en URL); bara hash lagras."""
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    from trixa_api.agent_auth import generate_token

    raw, token_hash, prefix = generate_token()
    client = get_postgrest()
    client.table("api_tokens").insert({
        "user_id": uid,
        "name": (name or "").strip() or "AI-token",
        "token_hash": token_hash,
        "token_prefix": prefix,
        "created_by": uid,
    }).execute()
    # Rendera settings med råtoken en gång (ej redirect → token läcker inte i URL).
    return settings_view(request, new_token=raw)


@router.post("/api-tokens/{token_id}/revoke")
def api_token_revoke(request: Request, token_id: str) -> Any:
    """Återkalla en token (scope:at till egen rad)."""
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    from datetime import datetime, timezone

    client = get_postgrest()
    client.table("api_tokens").update(
        {"revoked_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", token_id).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?saved=1", status_code=303)


_EXPERIENCE_LEVELS = {"beginner", "intermediate", "advanced", "elite"}
# Speglar CHECK-constraint på athlete_profiles.goal — andra värden avvisas av DB.
_GOALS = {"first_race", "pr", "ironman", "health", "comeback"}
# Tävlingsdistanser (athlete_profiles.race_type lagras som fritext men styrs
# av en lista så motorns label blir konsekvent). "ironman" = full 140.6.
# Enkelgrensdistanserna kom med migration 010 — Trixa är inte bara för triatleter.
_RACE_TYPES = {
    "sprint", "olympic", "half", "full", "ironman",
    "5k", "10k", "half_marathon", "marathon", "ultra",
    "gran_fondo", "time_trial", "stage_race",
    "open_water", "swim_meet",
    "other",
}


def _clean_pace(v: str | None) -> str | None:
    """mm:ss-ish fritext → trimmad sträng eller None. Ren passthrough, ingen
    validering utöver tomma värden (motorn tolkar zon-referenser separat)."""
    v = (v or "").strip()
    return v or None


@router.post("/settings/profile")
def settings_profile(
    request: Request,
    coach_name: str = Form(""),
    goal: str = Form(""),
    experience_level: str = Form(""),
    weekly_hours: float | None = Form(None),
    weekly_days: int | None = Form(None),
    race_type: str = Form(""),
    race_date: str = Form(""),
    time_goal: str = Form(""),
    ftp: int | None = Form(None),
    lthr: int | None = Form(None),
    swim_css: str = Form(""),
    run_threshold_pace: str = Form(""),
) -> Any:
    """Adeptens egen profil — måltävling, volym, erfarenhet, testvärden.

    Egen form (nollställer inte tränings-/anslutningsinställningar). Justerbar
    när som helst, inte bara vid onboarding. Tomma fält rörs inte destruktivt:
    text → NULL, tal → NULL, men befintliga värden skrivs över medvetet av det
    som skickas (formuläret är förifyllt så inget tappas av misstag).
    """
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    update: dict = {
        "coach_name": (coach_name or "").strip()[:40] or None,
        "weekly_hours": weekly_hours if weekly_hours and weekly_hours > 0 else None,
        "weekly_days": weekly_days if weekly_days and 1 <= weekly_days <= 7 else None,
        "race_type": race_type if race_type in _RACE_TYPES else None,
        "race_date": (race_date or "").strip() or None,
        "time_goal": (time_goal or "").strip() or None,
        "ftp": ftp if ftp and ftp > 0 else None,
        "lthr": lthr if lthr and lthr > 0 else None,
        "swim_css": _clean_pace(swim_css),
        "run_threshold_pace": _clean_pace(run_threshold_pace),
    }
    # goal och experience_level är NOT NULL + CHECK i DB: skriv dem bara när
    # formuläret skickar ett giltigt värde, annars behålls befintligt.
    if (goal or "").strip() in _GOALS:
        update["goal"] = goal.strip()
    if experience_level in _EXPERIENCE_LEVELS:
        update["experience_level"] = experience_level
    client = get_postgrest()
    client.table("athlete_profiles").update(update).eq("user_id", uid).execute()
    return RedirectResponse("/ui/settings?saved=1", status_code=303)


# ---------- Manuell passlogg (utfört → MASTER training_log, source='manual') ----------


@router.post("/log/session")
def log_session(
    request: Request,
    date: str = Form(...),
    sport: str = Form(...),
    duration_min: float | None = Form(None),
    distance_km: float | None = Form(None),
    avg_hr: int | None = Form(None),
    rpe: int | None = Form(None),
    title: str = Form(""),
    planned_session_id: str = Form(""),
) -> Any:
    """Logga ett utfört uthållighetspass manuellt. Förifyllt från planen i UI:t.

    Skriver MASTER training_log (source='manual'). Upsert på (user, datum, sport)
    så att korrigera ett loggat pass uppdaterar samma rad. Driver plan-vs-utfall
    även för adepter utan TP/Strava.
    """
    uid = _current_user_id(request)
    if not uid:
        raise HTTPException(401, "Inte inloggad")
    if sport not in ("swim", "bike", "run", "strength"):
        raise HTTPException(400, "Gren måste vara swim, bike, run eller strength")
    if not date:
        raise HTTPException(400, "datum krävs")
    client = get_postgrest()
    row: dict = {
        "user_id": uid, "date": date, "sport": sport,
        "title": (title or "").strip() or "Loggat pass", "source": "manual",
    }
    if duration_min is not None:
        row["duration_min"] = duration_min
    if distance_km is not None:
        row["distance_km"] = distance_km
    if avg_hr is not None:
        row["avg_hr"] = avg_hr
    if rpe is not None and 1 <= rpe <= 10:
        row["rpe"] = rpe
    if planned_session_id:
        row["planned_session_id"] = planned_session_id
    existing = (
        client.table("training_log").select("id")
        .eq("user_id", uid).eq("date", date).eq("sport", sport).eq("source", "manual")
        .execute()
    )
    if existing.data:
        client.table("training_log").update(row).eq("id", existing.data[0]["id"]).execute()
    else:
        client.table("training_log").insert(row).execute()
    return RedirectResponse("/ui/", status_code=303)


def _monday_of(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


# ---------- Dashboard ----------


# ---------- Säsongsplan (optimal fas+volym vs utfall) ----------

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


# ---------- Källdedup: ett fysiskt pass kan ligga som flera training_log-rader ----------
#
# Samma pass kan komma från TP-sync (source='tp'), Strava-backfill ('strava')
# och adeptens egen loggning ('manual') — utan att något fält länkar ihop dem
# (tp_workout_id ≠ strava_id ≠ NULL). Summeras alla rader blåses volym/load upp.
# Identitet = (datum, kanonisk gren, ~duration ±10%). Vid träff behålls EN rad
# enligt källprioritet: TP kanonisk (struktur/TSS), Strava backfill, manuellt sist.

_LOG_SOURCE_RANK = {"tp": 0, "strava": 1, "manual": 2}


def _canon_log_sport(sport: str | None) -> str:
    """training_log.sport är blandad vokabulär (Sim/swim/Simning). Normalisera
    så dedup matchar tvärs källor."""
    raw = (sport or "").strip()
    low = raw.lower()
    if raw in ("Löpning", "Lopning") or "run" in low or "löp" in low:
        return "run"
    if raw in ("Cykel", "Cykling") or "cycl" in low or "bike" in low or "ride" in low:
        return "bike"
    if raw in ("Sim", "Simning") or "swim" in low:
        return "swim"
    if raw == "Styrka" or "strength" in low or "weight" in low:
        return "strength"
    return low


def _dedup_log_rows(rows: list[dict]) -> list[dict]:
    """Behåll en rad per fysiskt pass (datum, gren, ~duration) — tp > strava > manual.

    Source-rank styr ordningen: TP-rader processas först → behålls; senare
    strava/manuella rader som matchar samma pass hoppas. Okänd källa sist.
    """
    kept: list[dict] = []
    for r in sorted(
        rows, key=lambda x: _LOG_SOURCE_RANK.get((x.get("source") or "").lower(), 3)
    ):
        day = str(r.get("date"))[:10]
        sport = _canon_log_sport(r.get("sport"))
        dur = float(_fnum(r.get("duration_min")) or 0)
        tol = max(2.0, dur * 0.10)
        if any(
            str(k.get("date"))[:10] == day
            and _canon_log_sport(k.get("sport")) == sport
            and abs(float(_fnum(k.get("duration_min")) or 0) - dur) <= tol
            for k in kept
        ):
            continue
        kept.append(r)
    return kept


# Trixa räknar styrke-, konditions- och yogapass. Allt annat (alpint, vandring,
# promenad, paddel …) ignoreras i volym/load/compliance. ("Yoga" → canon "yoga"
# via lowercase, så det räcker att ha med "yoga" här.)
_RELEVANT_SPORTS = {"run", "bike", "swim", "strength", "yoga"}


def _clean_log_rows(rows: list[dict]) -> list[dict]:
    """Förbered training_log-rader för aggregering: filtrera bort irrelevanta
    grenar (ej styrka/kondition) och deduppa källdubbletter (tp>strava>manual)."""
    relevant = [r for r in rows if _canon_log_sport(r.get("sport")) in _RELEVANT_SPORTS]
    return _dedup_log_rows(relevant)


def _compliance_by_week(client, athlete_id, today, user_id) -> dict:
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

    # Följsamhet mot MASTER training_log (alla källor: tp/strava/manuellt).
    activities_by_date: dict[str, list[dict]] = {}
    try:
        log_res = (
            client.table("training_log")
            .select("date, sport, title, duration_min, distance_km, avg_hr,"
                    " max_hr, avg_power, normalized_power, tss, source")
            .eq("user_id", user_id)
            .gte("date", window_start.isoformat()).lte("date", today.isoformat())
            .execute()
        )
        for r in _clean_log_rows(log_res.data or []):
            day = str(r.get("date"))[:10] if r.get("date") else None
            if day:
                activities_by_date.setdefault(day, []).append(_normalize_log_activity(r))
    except Exception:  # noqa: BLE001
        pass

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


def _add_week_hours(by_week: dict, day_iso: str, hours: float) -> None:
    try:
        d = date_type.fromisoformat(str(day_iso)[:10])
    except (ValueError, TypeError):
        return
    iso = d.isocalendar()
    by_week[(iso[0], iso[1])] = by_week.get((iso[0], iso[1]), 0.0) + hours


def _actual_hours_by_week(client, user_id, start, today) -> dict:
    """Faktisk veckovolym (h) ur MASTER training_log. {(iso_year, iso_week): h}.

    Samma källa som Nyckeltal-sidan, så säsongsvyn och nyckeltalen är överens.
    """
    out: dict[tuple[int, int], float] = {}
    if not user_id:
        return out
    try:
        res = (
            client.table("training_log")
            .select("date, sport, duration_min, source")
            .eq("user_id", user_id)
            .gte("date", start.isoformat())
            .lte("date", today.isoformat())
            .execute()
        )
    except Exception:  # noqa: BLE001
        return out
    for r in _clean_log_rows(res.data or []):
        _add_week_hours(out, r.get("date") or "", (_fnum(r.get("duration_min")) or 0) / 60.0)
    return out


def _planned_hours_by_week(client, user_id, start, end) -> dict:
    """Planerad veckovolym (h) ur MASTER planned_sessions. {(iso_year, iso_week): h}.

    Alla origins (nils/trixa2/manual) — det som faktiskt lagts i planen.
    Veckor utan plan saknas i mappen; säsongsvyn faller då tillbaka på
    projektionskurvan. Vila-rader (duration 0) påverkar inte summan.
    """
    out: dict[tuple[int, int], float] = {}
    if not user_id:
        return out
    try:
        res = (
            client.table("planned_sessions")
            .select("date, duration_min")
            .eq("user_id", user_id)
            .gte("date", start.isoformat())
            .lte("date", end.isoformat())
            .execute()
        )
    except Exception:  # noqa: BLE001
        return out
    for r in res.data or []:
        _add_week_hours(out, r.get("date") or "", (_fnum(r.get("duration_min")) or 0) / 60.0)
    return out


def _season_actuals(client, user_id, start, today):
    """Deduppade training_log-pass per ISO-vecka för säsongsvyn.

    Returnerar (hours_by_week, sessions_by_week) ur EN läsning. sessions_by_week
    matar de klickbara vecko-panelerna; hours_by_week matar volym + readiness.
    """
    hours: dict[tuple[int, int], float] = {}
    sessions: dict[tuple[int, int], list[dict]] = {}
    if not user_id:
        return hours, sessions
    try:
        res = (
            client.table("training_log")
            .select("date, sport, title, duration_min, distance_km, avg_hr, tss, source")
            .eq("user_id", user_id)
            .gte("date", start.isoformat())
            .lte("date", today.isoformat())
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return hours, sessions
    for r in _clean_log_rows(res.data or []):
        day = str(r.get("date"))[:10]
        try:
            d = date_type.fromisoformat(day)
        except (ValueError, TypeError):
            continue
        iso = d.isocalendar()
        key = (iso[0], iso[1])
        dur = _fnum(r.get("duration_min")) or 0.0
        hours[key] = hours.get(key, 0.0) + dur / 60.0
        sessions.setdefault(key, []).append({
            "date": day,
            "sport": _SPORT_LABEL.get(_canon_log_sport(r.get("sport")), r.get("sport") or "—"),
            "title": r.get("title") or "",
            "duration_min": int(round(dur)),
            "distance_km": _fnum(r.get("distance_km")),
            "avg_hr": r.get("avg_hr"),
            "tss": _fnum(r.get("tss")),
            "source": r.get("source"),
        })
    return hours, sessions


def _week_analysis(w: dict) -> str:
    """Deterministisk en-menings-analys per vecka (ingen LLM)."""
    opt = w.get("optimal_hours") or 0
    act = w.get("actual_hours")
    n = len(w.get("sessions") or [])
    phase = (w.get("phase_label") or "").lower()
    planned = w.get("optimal_source") == "plan"
    rec = " Vilovecka — medvetet lägre volym." if w.get("is_recovery") else ""
    if w.get("phase") == "transition":
        base = (f"Återhämtning efter tävling — lätt, valfri träning, "
                f"riktvolym max {opt} h.")
        if not w.get("future") and act:
            base += f" {act} h loggat."
        return base
    if w.get("future"):
        if planned:
            return f"Planerad vecka ({phase}) — {opt} h enligt lagd plan.{rec}"
        label = phase + (", underhållsnivå" if w.get("is_maintenance") else "")
        return f"Planerad vecka ({label}) — optimal riktvolym {opt} h.{rec}"
    if not act:
        ref = "Planerat" if planned else "Optimalt"
        return (f"Ingen träning loggad. {ref} {opt} h ({phase}).{rec}"
                if opt else "Ingen träning loggad den veckan.")
    txt = f"{act} h utfört mot {opt} h {'planerat' if planned else 'optimalt'}"
    if opt:
        pct = round((act / opt - 1) * 100)
        txt += f" ({'+' if pct > 0 else ''}{pct}%)"
    txt += f" · {n} pass."
    comp = w.get("compliance")
    if comp is None:
        txt += " Ingen plan den veckan att mäta mot."
    elif comp == "green":
        txt += " Följde planen väl."
    elif comp == "yellow":
        txt += " Delvis enligt plan."
    elif comp == "red":
        txt += " Avvek tydligt från plan."
    return txt + rec


def _build_season_context(client, athlete, today, this_monday) -> dict | None:
    """Säsongsplan: optimal fas+volym per vecka vs faktiskt utfall, eller None.

    Race läses från public.races (nästa A-race) — samma helper som planner —
    med fallback till athlete_profiles.race_date.
    """
    race_raw = None
    race_name = None
    last_race_d = None
    last_race_dist = None
    try:
        from coach.trixa.races import fetch_last_race, fetch_next_a_race

        race = fetch_next_a_race(client, athlete.get("id"), today)
        if race:
            race_raw = race.get("date")
            race_name = race.get("name")
        last = fetch_last_race(client, athlete.get("id"), today)
        if last:
            last_race_dist = last.get("distance")
            try:
                last_race_d = date_type.fromisoformat(str(last.get("date"))[:10])
            except (ValueError, TypeError):
                last_race_d = None
    except Exception:  # noqa: BLE001 — races-tabell saknas/fel → fallback
        race_raw = None
    if not race_raw:
        race_raw = athlete.get("race_date")
    if not race_raw:
        return None
    try:
        race_d = date_type.fromisoformat(str(race_raw)[:10])
    except (ValueError, TypeError):
        return None

    user_id = athlete.get("user_id")
    peak_hours = float(athlete.get("weekly_hours") or 0) or 12.0

    # Faktiskt utfört per vecka ur MASTER training_log (deduppat). Brett fönster
    # (28 v) så HELA den optimala periodiseringen täcks, inte bara lookbacken.
    window_start = this_monday - timedelta(weeks=28)
    actual_by_week, sessions_by_week = _season_actuals(client, user_id, window_start, today)

    try:
        comp_map = _compliance_by_week(
            client, athlete["id"], today, user_id,
        )
    except Exception:  # noqa: BLE001
        comp_map = {}

    # Veckor med en faktisk plan visar plannerns/Nils beslut som "optimal"
    # istället för projektionskurvan — framåt till race så nästa genererade
    # vecka också syns.
    planned_by_week = _planned_hours_by_week(client, user_id, window_start, race_d)

    plan = season.build_season_plan(
        today, race_d, peak_hours, actual_by_week, comp_map,
        athlete=athlete, planned_by_week=planned_by_week,
        last_race_date=last_race_d, last_race_distance=last_race_dist,
    )
    if not plan:
        return None
    plan["race_date"] = race_d.isoformat()
    plan["race_label"] = (
        race_name
        or season.race_label(race_d)
        or (athlete.get("race_type") or "Tävling").capitalize()
    )
    plan["races"] = season.race_milestones(plan)

    # Klickbara veckor: bädda in utförda pass + deterministisk analys per vecka.
    for w in plan["weeks"]:
        w["sessions"] = sessions_by_week.get((w["iso_year"], w["iso_week"]), [])
        w["analysis"] = _week_analysis(w)

    # Readiness-projektion + ramp-vakt — nu på samma volymkälla (training_log).
    def _wk(i: int) -> float:
        iso = (this_monday - timedelta(weeks=i)).isocalendar()
        return round(actual_by_week.get((iso[0], iso[1]), 0.0), 1)

    series = [_wk(i) for i in range(6, 0, -1)]  # 6 avslutade veckor, äldst→nyast
    recent = series[-4:]
    current_h = round(sum(recent) / len(recent), 1) if recent else 0.0
    proj = readiness.build_projection(current_h, plan["weeks_to_race"])
    plan["readiness"] = {
        "current_hours": proj.current_hours,
        "base_eta": proj.base_eta,
        "build_eta": proj.build_eta,
        "base_eta_positive": proj.base_eta is not None and proj.base_eta > 0,
        "build_eta_positive": proj.build_eta is not None and proj.build_eta > 0,
        "ramp_pct": proj.ramp_pct,
        "on_track": proj.on_track,
        "verdict": proj.verdict,
        "ramp_flag": readiness.ramp_flag(series),
    }
    return plan


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()

    a_res = client.table("athlete_profiles").select("*").eq("user_id", user_id).execute()
    athlete = a_res.data[0] if a_res.data else None

    if not athlete:
        # Säkerhetsnät: användare skapad utanför signup-flödet — skapa raden
        # och skicka till onboarding.
        try:
            _ensure_athlete_profile(client, user_id)
            return RedirectResponse(url="/ui/onboarding", status_code=303)
        except Exception:  # noqa: BLE001
            return _render("dashboard.html", {
                "request": request, "athlete": None,
                "this_week": None, "next_week": None, "alerts": [],
                "timeline": None,
            })

    if not athlete.get("onboarded_at"):
        return RedirectResponse(url="/ui/onboarding", status_code=303)

    # Hämta båda veckorna från DB
    today = date_type.today()
    this_monday = _monday_of(today)
    next_monday = this_monday + timedelta(days=7)
    this_iso = this_monday.isocalendar()
    next_iso = next_monday.isocalendar()

    # Allt utfört läses ur MASTER training_log (i _fetch_current_week_data) →
    # ingen garmin/strava-källgating behövs. Hårdat: en trasig vecka fäller
    # inte hela dashboarden.
    uid = athlete.get("user_id")
    try:
        this_week = _fetch_current_week_data(
            client, athlete["id"], this_iso[0], this_iso[1], today, uid
        )
    except Exception:  # noqa: BLE001
        this_week = None
    try:
        next_week = _fetch_current_week_data(
            client, athlete["id"], next_iso[0], next_iso[1], today, uid
        )
    except Exception:  # noqa: BLE001
        next_week = None

    # Hämta alerts. Dashboarden får inte fälla hela UI:t om en äldre
    # livedatabas saknar något alert-fält.
    try:
        alerts = (
            client.table("coach_alerts")
            .select("*")
            .eq("athlete_id", user_id)
            .eq("is_dismissed", False)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        ).data or []
    except Exception:  # noqa: BLE001
        alerts = []

    # Lägg på namn för välkomst
    try:
        profile_res = client.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        if profile_res.data:
            profile = profile_res.data[0]
            athlete["name"] = (
                profile.get("name")
                or profile.get("full_name")
                or profile.get("display_name")
                or profile.get("email")
            )
    except Exception:  # noqa: BLE001
        athlete.setdefault("name", None)

    # Hämta engine-fas för alternative-uppslag
    phase = None
    period = None
    optimal_phase = None
    behind = False
    try:
        from coach.trixa.planner import _build_athlete_state, _build_ot_signals, _run_engine
        state = _build_athlete_state(athlete, None, today)
        decisions = _run_engine(state, _build_ot_signals(athlete, None), 1, 6)
        phase_rec = decisions.get("phase_recommendation") or {}
        phase = phase_rec.get("phase")
        period = phase_rec.get("period")
        optimal_phase = phase_rec.get("optimal_phase")
        behind = phase_rec.get("behind", False)
    except Exception:  # noqa: BLE001
        pass

    # Säsongs-tidslinje: fas-staplar bakåt från race + följsamhet per vecka
    try:
        timeline = _build_season_context(client, athlete, today, this_monday)
    except Exception:  # noqa: BLE001
        timeline = None

    context = {
        "request": request,
        "athlete": athlete,
        "this_week": this_week,
        "next_week": next_week,
        "alerts": alerts,
        "phase": phase,
        "optimal_phase": optimal_phase,
        "behind": behind,
        "this_monday": this_monday.isoformat(),
        "next_monday": next_monday.isoformat(),
        "timeline": timeline,
        "conn_ai": bool(athlete.get("conn_ai")),
    }
    try:
        return _render("dashboard.html", context)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            content=(
                "<!doctype html><html lang='sv'><meta charset='utf-8'>"
                "<title>Trixa</title><body>"
                "<h1>Trixa</h1>"
                "<p>Dashboarden kunde inte renderas just nu, men API och sync är igång.</p>"
                f"<pre>{str(exc)}</pre>"
                "<p><a href='/ui/settings'>Inställningar</a> · "
                "<a href='/ui/report'>Veckorapport</a> · "
                "<a href='/health/integrations'>Integrationshälsa</a></p>"
                "</body></html>"
            ),
            status_code=200,
        )


# ---------- Plan vs actual: matchning mot MASTER training_log ----------
#
# Ren, deterministisk matchning. Inga LLM-anrop. Statusen per pass räknas ut
# från passets datum vs idag + matchande aktivitet i public.training_log.

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
    dist_km = round(_safe_float(dist_m) / 1000, 1) if dist_m else None
    watts = np_watt or avg_power

    parts = [f"{dur_min} min"]
    if avg_hr:
        parts.append(f"{avg_hr} bpm")
    if sport == "bike" and watts:
        parts.append(f"{watts} W")
    if dist_km:
        parts.append(f"{dist_km} km")
    if load:
        parts.append(f"TSS {round(_safe_float(load))}")

    return {
        "summary": "Genomfört: " + " · ".join(parts),
        "name": act.get("activity_name"),
        "activity_type": act.get("activity_type"),
        "duration_min": dur_min,
        "avg_hr": avg_hr,
        "max_hr": act.get("max_hr"),
        "training_load": round(_safe_float(load)) if load else None,
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
    planned_session_id: str | None = None,
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
            linked = [
                a for a in day_activities
                if planned_session_id
                and str(a.get("planned_session_id")) == str(planned_session_id)
            ]
            sport_hits = [a for a in day_activities if _sport_matches(sport, code, a.get("_sport"))]
            pool = linked or sport_hits or day_activities
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
    linked = [
        a for a in day_activities
        if planned_session_id
        and str(a.get("planned_session_id")) == str(planned_session_id)
    ]
    sport_hits = [a for a in day_activities if _sport_matches(sport, code, a.get("_sport"))]
    pool = linked or sport_hits or day_activities
    best = min(pool, key=lambda a: abs(a["_dur_min"] - (plan_min or 0)))

    sport_ok = bool(linked) or _sport_matches(sport, code, best.get("_sport"))
    dur_ok = _within_duration_tolerance(plan_min, best["_dur_min"])
    actual = _build_actual(best, best.get("_sport") or sport)

    if sport_ok and dur_ok:
        return {**_STATUS["done"], "key": "done", "actual": actual}
    return {**_STATUS["deviated"], "key": "deviated", "actual": actual}


def _normalize_training_log_activity(row: dict) -> dict:
    """En training_log-rad → formen som status/rendering konsumerar."""
    dur_min = _safe_float(row.get("duration_min"))
    dist_km = row.get("distance_km")
    return {
        "_sport": _PLANNED_SV_SPORT.get(
            row.get("sport"), (row.get("sport") or "").strip().lower()
        ),
        "_dur_min": dur_min,
        "activity_name": row.get("title"),
        "activity_type": row.get("sport"),
        "duration_sec": int(round(dur_min * 60)),
        "avg_hr": row.get("avg_hr"),
        "max_hr": row.get("max_hr"),
        "training_load": row.get("tss"),
        "distance_m": round(_safe_float(dist_km) * 1000) if dist_km else None,
        "normalized_power": row.get("normalized_power"),
        "avg_power": row.get("avg_power"),
        "start_time_local": row.get("date"),
        "planned_session_id": row.get("planned_session_id"),
        "source": row.get("source"),
    }


def _fetch_week_activities(
    client, user_id: str | None, week_monday: date_type
) -> dict[str, list[dict]]:
    """Hämta utförda pass från MASTER training_log, grupperade per datum."""
    if not user_id:
        return {}
    week_start = week_monday.isoformat()
    week_end = (week_monday + timedelta(days=7)).isoformat()
    try:
        res = (
            client.table("training_log")
            .select(
                "date,sport,title,duration_min,distance_km,avg_hr,max_hr,"
                "avg_power,normalized_power,tss,source,planned_session_id"
            )
            .eq("user_id", user_id)
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
        by_date.setdefault(day, []).append(_normalize_training_log_activity(row))
    return by_date


# Svenska sportnamn i planned_sessions (coach/Nils) → Trixas discipliner.
_PLANNED_SV_SPORT = {
    "Cykel": "bike", "Löpning": "run", "Lopning": "run",
    "Simning": "swim", "Sim": "swim", "Styrka": "strength",
    "Vila": "rest", "Yoga": "rest", "Promenad": "rest", "Vandring": "rest",
}


def _normalize_log_activity(row: dict) -> dict:
    """En manuell training_log-rad → samma form som _compute_status konsumerar."""
    dur_min = float(_fnum(row.get("duration_min")) or 0)
    dist_km = _fnum(row.get("distance_km"))
    return {
        "_sport": _TL_SPORT.get((row.get("sport") or "").strip().lower()),
        "_dur_min": dur_min,
        "activity_name": row.get("title"),
        "activity_type": row.get("sport"),
        "duration_sec": int(round(dur_min * 60)),
        "avg_hr": row.get("avg_hr"),
        "max_hr": row.get("max_hr"),
        "training_load": _fnum(row.get("tss")),
        "distance_m": round(dist_km * 1000) if dist_km else None,
        "normalized_power": row.get("normalized_power"),
        "avg_power": row.get("avg_power"),
        "start_time_local": str(row.get("date"))[:10],
        "_manual": True,
    }


def _fetch_completed_week(client, user_id, week_monday) -> dict[str, list[dict]]:
    """Utfört för veckan ur MASTER training_log — ALLA källor (tp/strava/manuellt).

    training_log är deduppat och källtaggat med rika fält (distans/puls/watt/TSS).
    Ersätter den sparsa garmin_coach.activities-cachen som bara hade tid och
    därför gav fattiga "Genomfört: X min"-rader.
    """
    if not user_id:
        return {}
    start = week_monday.isoformat()
    end = (week_monday + timedelta(days=7)).isoformat()
    try:
        res = (
            client.table("training_log")
            .select("date, sport, title, duration_min, distance_km, avg_hr,"
                    " max_hr, avg_power, normalized_power, tss, source")
            .eq("user_id", user_id)
            .gte("date", start)
            .lt("date", end)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return {}
    by_date: dict[str, list[dict]] = {}
    for r in _clean_log_rows(res.data or []):
        day = str(r.get("date"))[:10] if r.get("date") else None
        if day:
            by_date.setdefault(day, []).append(_normalize_log_activity(r))
    return by_date


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
                " steps, exercises, origin, workout_code, intensity, status"
            )
            .eq("user_id", user_id)
            .gte("date", start)
            .lte("date", end)
            .order("date")
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    active = [row for row in (res.data or []) if row.get("status") != "cancelled"]
    return active or None


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
        # Förberäknad bool så templaten slipper jämföra `sets > 1` (dict → krasch).
        sets_val = c.get("sets")
        c["sets_gt_one"] = isinstance(sets_val, (int, float)) and sets_val > 1
        out.append(c)
    return out


def _fetch_current_week_data(
    client,
    athlete_id: str,
    year: int,
    week_num: int,
    today: date_type | None = None,
    user_id: str | None = None,
) -> dict | None:
    """Veckans plan + plan-vs-actual.

    Planerad källa: MASTER planned_sessions. Utfört: MASTER training_log.
    """
    if today is None:
        today = date_type.today()
    week_monday = date_type.fromisocalendar(year, week_num, 1)

    # Utfört (plan-vs-actual) läses ur MASTER training_log — alla källor
    # (tp/strava/manuellt), rika fält. Hoppas över för rena framtidsveckor.
    activities_by_date: dict[str, list[dict]] = {}
    if user_id and week_monday <= today:
        activities_by_date = _fetch_completed_week(client, user_id, week_monday)

    # MASTER: planen läses från planned_sessions (docs/08). Raderna kan komma
    # från Nils (origin='nils'), motorn (origin='trixa2') eller legacy (NULL).
    sessions = _fetch_planned_sessions_week(client, user_id, week_monday)
    if not sessions:
        return None

    has_coach_rows = any((ps.get("origin") or "") == "nils" for ps in sessions)
    has_engine_rows = any((ps.get("origin") or "") == "trixa2" for ps in sessions)
    if has_coach_rows and has_engine_rows:
        plan_source = "mixed"
    elif has_coach_rows:
        plan_source = "coach"
    else:
        plan_source = "engine"

    week = {
        "id": None,
        "week_start": week_monday.isoformat(),
        "week_end": (week_monday + timedelta(days=6)).isoformat(),
        "phase": None,
        "plan_source": plan_source,
        "workouts": [],
    }

    def _status(d, sport, code, dur, session_id):
        return _compute_status(
            d,
            sport,
            code,
            dur,
            activities_by_date.get(str(d)[:10], []),
            today,
            session_id,
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
            "status": _status(ps["date"], sport, code or title, dur, ps.get("id")),
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

_LOCATION_LABELS = dict(_BODY_LOCATIONS)


def _public_base_url(request: Request) -> str:
    """Adressen adepten ska peka sin AI-klient på.

    Bakom Railways proxy stämmer inte alltid ``request.base_url`` (den kan bli
    http:// eller den interna porten), så TRIXA_PUBLIC_URL vinner när den är satt.
    """
    configured = os.environ.get("TRIXA_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _location_text(concern: dict) -> str:
    """Kroppsdelar för ett besvär som läsbar svensk text.

    Besvär kan sitta på flera ställen samtidigt (bägge knäna, båda hälsenorna).
    Nya rader bär ``locations`` som lista; äldre rader har bara ``location``
    som enskild nyckel — båda ska visas.
    """
    keys = concern.get("locations") or ([concern["location"]] if concern.get("location") else [])
    labels = [_LOCATION_LABELS.get(k, k) for k in keys if k]
    return ", ".join(labels)


# ---------- Onboarding: valen som styr hur formuläret ser ut ----------

# Coachens namn är adeptens val, inte ett produktnamn. Personligheterna skiljer
# sig i ton, inte i träningslära — motorn är densamma bakom alla.
_COACH_NAMES = [
    ("Nils", "lugn och rak, förklarar sällan två gånger"),
    ("Maja", "driver på, tar i när du har marginal"),
    ("Anders", "teknikfokuserad, bryr sig om detaljerna"),
    ("Elin", "pedagogisk, motiverar varje pass"),
    ("Sam", "kort och konkret, inga utsvävningar"),
]

# Tävlingsdistanser per gren. ``requires`` styr när alternativet visas:
# "tri" = adepten har sim + cykel + löpning igång, annars grenens egen kod.
# Speglar CHECK-constraint på races.distance (migration 010).
_RACE_DISTANCES = [
    ("sprint", "Triathlon sprint", "tri"),
    ("olympic", "Triathlon olympisk", "tri"),
    ("half", "Triathlon halv (70.3)", "tri"),
    ("full", "Triathlon hel (140.6)", "tri"),
    ("5k", "5 km", "run"),
    ("10k", "10 km", "run"),
    ("half_marathon", "Halvmaraton", "run"),
    ("marathon", "Maraton", "run"),
    ("ultra", "Ultralopp", "run"),
    ("gran_fondo", "Långlopp", "bike"),
    ("time_trial", "Tempolopp", "bike"),
    ("stage_race", "Etapplopp", "bike"),
    ("open_water", "Öppet vatten", "swim"),
    ("swim_meet", "Bassängtävling", "swim"),
    ("other", "Annat", "any"),
]

_RACE_DISTANCE_VALUES = {value for value, _label, _req in _RACE_DISTANCES}

# Långpassdag är bara meningsfull för uthållighetsgrenarna.
_LONG_DAY_FIELDS = [
    ("long_bike_day", "bike", "Dag för långt cykelpass"),
    ("long_run_day", "run", "Dag för långt löppass"),
]

# Tröskelfält per gren — en simmare ska inte mötas av FTP-rutan.
_THRESHOLD_FIELDS = [
    ("ftp", "bike", "FTP cykel (watt)", "t.ex. 220", "number"),
    ("lthr_bike", "bike", "Tröskelpuls cykel (bpm)", "t.ex. 158", "number"),
    ("run_threshold_pace", "run", "Tröskelpace löpning (min:sek per km)", "t.ex. 5:00", "pace"),
    ("lthr", "run", "Tröskelpuls löpning (bpm)", "t.ex. 165", "number"),
    ("swim_css", "swim", "CSS simning (min:sek per 100 m)", "t.ex. 2:00", "pace"),
]

# Bumpas när formuläret får nya frågor, så vi kan efterfråga bara det som
# saknas istället för att köra om hela onboardingen.
ONBOARDING_VERSION = 1


# ---------- Onboarding (första inloggningen — grunddata för planeringen) ----------


@router.get("/onboarding", response_class=HTMLResponse)
def onboarding_form(request: Request, error: str = "") -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    athlete = _ensure_athlete_profile(client, user_id)

    from coach.engine._loader import load_yaml

    try:
        nutrition_defaults = load_yaml("nutrition.yaml").get("defaults", {})
    except Exception:  # noqa: BLE001
        nutrition_defaults = {"race_carbs_per_hour_g": 90, "carb_load_g_per_kg_per_day": 10}

    return _render(
        "onboarding.html",
        {
            "request": request,
            "athlete": athlete,
            "days": _DAY_LABELS,
            "locations": _BODY_LOCATIONS,
            "sports_options": _SPORT_OPTIONS,
            "base_url": _public_base_url(request),
            "coach_names": _COACH_NAMES,
            "race_distances": _RACE_DISTANCES,
            "long_day_fields": _LONG_DAY_FIELDS,
            "threshold_fields": _THRESHOLD_FIELDS,
            "nutrition_defaults": nutrition_defaults,
            "error": error,
        },
    )


@router.post("/onboarding")
def onboarding_submit(
    request: Request,
    coach_name: str = Form(""),
    coach_name_custom: str = Form(""),
    sports: list[str] = Form(default=[]),
    goal: str = Form(""),
    experience_level: str = Form(""),
    ftp: str = Form(""),
    lthr_bike: str = Form(""),
    run_threshold_pace: str = Form(""),
    lthr: str = Form(""),
    swim_css: str = Form(""),
    max_hr: str = Form(""),
    resting_hr: str = Form(""),
    threshold_source: str = Form("estimate"),
    race_name: str = Form(""),
    race_date: str = Form(""),
    race_distance: str = Form(""),
    race_priority: str = Form("A"),
    race_target: str = Form(""),
    weekly_hours: str = Form("8"),
    long_bike_day: str = Form(""),
    long_run_day: str = Form(""),
    rest_days: list[str] = Form(default=[]),
    recovery_week_ratio: str = Form("3:1"),
    concern_name_1: str = Form(""),
    concern_locations_1: list[str] = Form(default=[]),
    concern_severity_1: str = Form("2"),
    concern_name_2: str = Form(""),
    concern_locations_2: list[str] = Form(default=[]),
    concern_severity_2: str = Form("2"),
    concern_name_3: str = Form(""),
    concern_locations_3: list[str] = Form(default=[]),
    concern_severity_3: str = Form("2"),
    condition_name_1: str = Form(""),
    condition_medication_1: str = Form(""),
    condition_name_2: str = Form(""),
    condition_medication_2: str = Form(""),
    race_carbs: str = Form(""),
    nutrition_notes: str = Form(""),
) -> Any:
    user_id = _current_user_id(request)
    client = get_postgrest()
    athlete = _ensure_athlete_profile(client, user_id)
    athlete_id = athlete["id"]

    def _int_or_none(v: str) -> int | None:
        try:
            return int(v) if str(v).strip() else None
        except (ValueError, TypeError):
            return None

    def _float_or_none(v: str) -> float | None:
        try:
            return float(str(v).replace(",", ".")) if str(v).strip() else None
        except (ValueError, TypeError):
            return None

    source = threshold_source if threshold_source in ("estimate", "test") else "estimate"
    today_iso = date_type.today().isoformat()
    threshold_meta = {
        key: {"source": source, "tested_at": today_iso}
        for key, filled in (
            ("ftp", ftp), ("css", swim_css), ("run_pace", run_threshold_pace)
        )
        if str(filled).strip()
    }

    # Aktiva discipliner styr resten av formuläret. Tom lista = adepten kryssade
    # ur allt; då är sim/cykel/löpning en rimligare utgångspunkt än ingenting.
    valid_sports = [s for s, _label in _SPORT_OPTIONS]
    active_sports = [s for s in sports if s in valid_sports] or ["swim", "bike", "run"]

    chosen_coach = (coach_name_custom.strip() or coach_name.strip())[:40] or None

    update: dict = {
        "coach_name": chosen_coach,
        "sports": active_sports,
        "ftp": _int_or_none(ftp),
        "lthr_bike": _int_or_none(lthr_bike),
        "lthr": _int_or_none(lthr),
        "max_hr": _int_or_none(max_hr),
        "resting_hr": _int_or_none(resting_hr),
        "run_threshold_pace": run_threshold_pace.strip() or None,
        "swim_css": swim_css.strip() or None,
        "threshold_meta": threshold_meta,
        "weekly_hours": _float_or_none(weekly_hours) or 6,
        # Långpassdagar hör till grenar adepten faktiskt kör — en ren simmare
        # ska inte få ett cykel-långpass inbokat för att fältet råkade skickas.
        "long_bike_day": (long_bike_day or None) if "bike" in active_sports else None,
        "long_run_day": (long_run_day or None) if "run" in active_sports else None,
        "preferred_rest_days": [d for d in rest_days if d in dict(_DAY_LABELS)],
        "recovery_week_ratio": (
            recovery_week_ratio if recovery_week_ratio in ("3:1", "2:1") else "3:1"
        ),
        "race_carbs_per_hour_g": _int_or_none(race_carbs),
        "nutrition_notes": nutrition_notes.strip(),
        "onboarded_at": datetime.now(timezone.utc).isoformat(),
        "onboarding_version": ONBOARDING_VERSION,
    }

    # goal och experience_level är NOT NULL + CHECK i DB: skriv bara giltiga
    # värden, annars behålls raden's defaults (ironman/intermediate).
    if (goal or "").strip() in _GOALS:
        update["goal"] = goal.strip()
    if experience_level in _EXPERIENCE_LEVELS:
        update["experience_level"] = experience_level

    # Hälsa — samma jsonb-strukturer som Hälsa-sidan använder. Ett besvär kan
    # sitta på flera ställen (bägge knäna) och adepten kan ha flera besvär
    # samtidigt, så både platser och rader är listor.
    concerns = []
    for name, locations, severity in (
        (concern_name_1, concern_locations_1, concern_severity_1),
        (concern_name_2, concern_locations_2, concern_severity_2),
        (concern_name_3, concern_locations_3, concern_severity_3),
    ):
        if not name.strip():
            continue
        locs = [loc for loc in locations if loc in _LOCATION_LABELS] or ["other"]
        concerns.append({
            "name": name.strip(),
            # location (singular) behålls för äldre läsvägar; locations är sanningen
            "location": locs[0],
            "locations": locs,
            "severity": _int_or_none(severity) or 2,
            "since_date": today_iso,
            "needs_followup": False,
            "follow_up_by": None,
            "notes": None,
            "impact_per_discipline": {},
        })
    if concerns:
        update["active_concerns"] = (athlete.get("active_concerns") or []) + concerns

    conditions = []
    for name, medication in (
        (condition_name_1, condition_medication_1),
        (condition_name_2, condition_medication_2),
    ):
        if not name.strip():
            continue
        conditions.append({
            "name": name.strip(),
            "medication": medication.strip() or None,
            "dose": None,
            "diagnosed_year": None,
            "notes": None,
        })
    if conditions:
        update["health_conditions"] = (athlete.get("health_conditions") or []) + conditions

    client.table("athlete_profiles").update(update).eq("id", athlete_id).execute()

    # Målet → races-kalendern (denormaliserad kopia i race_date behålls som
    # fallback för äldre läsvägar). Distansen är grenberoende sedan migration
    # 010 — en löpare lägger in "marathon", inte en triathlondistans.
    distance = race_distance if race_distance in _RACE_DISTANCE_VALUES else "other"
    if race_name.strip() and race_date.strip():
        try:
            client.table("races").insert({
                "athlete_id": athlete_id,
                "name": race_name.strip(),
                "date": race_date.strip(),
                "distance": distance,
                "priority": race_priority if race_priority in ("A", "B", "C") else "A",
                "target_total": race_target.strip() or None,
            }).execute()
            client.table("athlete_profiles").update({
                "race_date": race_date.strip(),
                "race_type": "ironman" if distance == "full" else distance,
            }).eq("id", athlete_id).execute()
        except Exception:  # noqa: BLE001 — tävlingen kan läggas till senare
            logger.exception("Kunde inte spara tävlingen från onboardingen")

    return RedirectResponse(url="/ui/onboarding/klart", status_code=303)


@router.get("/onboarding/klart", response_class=HTMLResponse)
def onboarding_done(request: Request) -> HTMLResponse:
    """Kvittens efter onboardingen: vad Trixa nu vet, och vad som händer sedan.

    Formuläret är långt och en rak redirect till dashboarden gjorde det omöjligt
    att se om något faktiskt fastnade. Här speglas svaren tillbaka, med länkar
    till där de ändras.
    """
    user_id = _current_user_id(request)
    client = get_postgrest()
    athlete = _ensure_athlete_profile(client, user_id)

    races = []
    try:
        res = (
            client.table("races")
            .select("name, date, distance, priority, target_total")
            .eq("athlete_id", athlete["id"])
            # Kvittensen ska visa vad adepten tränar MOT, inte gamla lopp som
            # råkar ligga kvar i kalendern.
            .gte("date", date_type.today().isoformat())
            .order("date")
            .execute()
        )
        races = res.data or []
    except Exception:  # noqa: BLE001 — kvittensen får inte falla på en tom kalender
        logger.exception("Kunde inte läsa races till onboarding-kvittensen")

    sport_labels = dict(_SPORT_OPTIONS)
    distance_labels = {value: label for value, label, _req in _RACE_DISTANCES}
    active = athlete.get("sports") or []

    return _render(
        "onboarding_done.html",
        {
            "request": request,
            "athlete": athlete,
            "coach_name": athlete.get("coach_name") or "Din coach",
            "sport_labels": [sport_labels.get(s, s) for s in active],
            "active_sports": active,
            "races": races,
            "distance_labels": distance_labels,
            "day_labels": dict(_DAY_LABELS),
            "concerns": athlete.get("active_concerns") or [],
            "conditions": athlete.get("health_conditions") or [],
            "location_text": _location_text,
            "has_thresholds": any(
                athlete.get(field)
                for field in ("ftp", "lthr", "lthr_bike", "swim_css", "run_threshold_pace")
            ),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_view(
    request: Request, saved: bool = False, strava: str = "", why: str = "", tp: str = "",
    new_token: str = "",
) -> HTMLResponse:
    user_id = _current_user_id(request)
    client = get_postgrest()
    a_res = (
        client.table("athlete_profiles")
        .select(
            "sports, long_bike_day, long_run_day, preferred_rest_days,"
            " equipment, preferred_settings, use_strava, garmin_athlete_id,"
            " conn_ai, conn_tp, conn_strava, coach_name,"
            " goal, experience_level, weekly_hours, weekly_days, race_type,"
            " race_date, time_goal, ftp, lthr, swim_css, run_threshold_pace"
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

    # API-tokens (per-adept, för extern AI/Nils) — lista aktiva (ej råvärde)
    try:
        tok_res = (
            client.table("api_tokens")
            .select("id, name, token_prefix, created_at, last_used_at")
            .eq("user_id", user_id).is_("revoked_at", "null")
            .order("created_at", desc=True).execute()
        )
        api_tokens = tok_res.data or []
    except Exception:  # noqa: BLE001
        api_tokens = []

    # TP-anslutningsstatus (finns en cookie sparad för användaren?)
    try:
        tp_row = client.table("tp_auth").select("user_id").eq("user_id", user_id).limit(1).execute()
        tp_connected = bool(tp_row.data)
    except Exception:  # noqa: BLE001
        tp_connected = False

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
        "tp_flash": tp,
        "tp_connected": tp_connected,
    }
    return _render(
        "settings.html",
        {
            "request": request,
            "athlete": athlete,
            "days": _DAY_LABELS,
            "sports_options": _SPORT_OPTIONS,
            "base_url": _public_base_url(request),
            "disciplines_for_setting": [
                ("swim", "Simning"),
                ("bike", "Cykel"),
                ("run", "Löpning"),
            ],
            "saved": saved,
            "strava": strava_status,
            "api_tokens": api_tokens,
            "new_token": new_token,
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
    "yoga": "yoga",
}
_SPORT_LABEL = {
    "swim": "Simning", "bike": "Cykel", "run": "Löpning", "yoga": "Yoga",
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
            .select("date, sport, duration_min, distance_km, tss, source")
            .eq("user_id", user_id)
            .gte("date", window_start.isoformat())
            .lte("date", today.isoformat())
            .order("date")
            .execute()
        )
        # Filtrera irrelevanta grenar + deduppa källdubbletter (tp>strava>manual).
        rows = _clean_log_rows(res.data or [])
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
        # Pixelhöjd (max 56 px) — procenthöjd kollapsar i flex utan förälderhöjd.
        s["px"] = max(3, round(s["hours"] / max_h * 56)) if max_h > 0 else 3

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
            "location_text": _location_text,
            "disciplines": _DISCIPLINES_FOR_IMPACT,
            "added": added,
        },
    )


@router.post("/health/add", response_class=HTMLResponse)
def health_add(
    request: Request,
    name: str = Form(...),
    locations: list[str] = Form(default=[]),
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

    # Ett besvär kan sitta på flera ställen samtidigt (bägge knäna). location
    # (singular) skrivs kvar för äldre läsvägar; locations är sanningen.
    locs = [loc for loc in locations if loc in _LOCATION_LABELS]
    new_concern = {
        "name": name,
        "location": locs[0] if locs else None,
        "locations": locs,
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
    """Regenerera motorns del av veckan. Nils/manuella rader bevaras."""
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
def workout_swap(request: Request, workout_id: str) -> Any:
    """Byt motorpass till ett annat pass i samma gren/kategori."""
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Inte inloggad")
    try:
        swap_workout_to_next_alternative(workout_id, user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url="/ui/", status_code=303)


@router.post("/workouts/{workout_id}/change-discipline", response_class=HTMLResponse)
def workout_change_discipline(
    request: Request,
    workout_id: str,
    discipline: str = Form(...),
) -> Any:
    """Byt gren för passet och planera om återstående motorpass i veckan."""
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Inte inloggad")
    try:
        swap_workout_discipline_and_replan(
            workout_id, discipline, user_id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url="/ui/", status_code=303)


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
    user_id = _current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Inte inloggad")
    client = get_postgrest()
    (
        client.table("planned_sessions")
        .delete()
        .eq("id", workout_id)
        .eq("user_id", user_id)
        .eq("origin", "manual")
        .execute()
    )
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
    current_user_id = _current_user_id(request)
    if not current_user_id:
        raise HTTPException(401, "Inte inloggad")
    if athlete_user_id != current_user_id:
        raise HTTPException(403, "Du kan bara generera din egen plan")
    try:
        ws = date_type.fromisoformat(week_start)
        plan = generate_week(
            athlete_user_id=current_user_id,
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
            "default_user_id": current_user_id,
            "default_week_start": week_start,
            "result": plan,
            "result_json": json.dumps(result_dict, indent=2, ensure_ascii=False, default=str),
        })
