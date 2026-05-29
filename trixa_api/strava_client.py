"""Strava OAuth + aktivitetssynk för Trixa2.

Bantad port av original-Trixas integrations/strava.py. Stateless HTTP +
token-lagring i public.strava_tokens, aktiviteter i public.strava_activities
(samma tabeller som dashboarden redan läser källagnostiskt). Ingen LLM.

Creds: STRAVA_CLIENT_ID/SECRET (env). HMAC-state-nyckel: STRAVA_STATE_SECRET
eller (fallback) service-role-nyckeln.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests

from coach.trixa.db import _load_env

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
_TIMEOUT = 30

# Strava activity_type → svenska namn (som dashboardens _STRAVA_TYPE_TO_SPORT förstår)
SPORT_MAP = {
    "Run": "Lopning", "TrailRun": "Lopning", "VirtualRun": "Lopning",
    "Ride": "Cykel", "VirtualRide": "Cykel", "EBikeRide": "Cykel",
    "MountainBikeRide": "Cykel", "GravelRide": "Cykel",
    "Swim": "Sim", "OpenWaterSwim": "Sim",
    "WeightTraining": "Styrka", "Workout": "Styrka", "Yoga": "Yoga",
}


def _creds() -> tuple[str, str]:
    _load_env()
    return (
        os.environ.get("STRAVA_CLIENT_ID", ""),
        os.environ.get("STRAVA_CLIENT_SECRET", ""),
    )


def creds_configured() -> bool:
    cid, secret = _creds()
    return bool(cid and secret)


def _state_secret() -> str:
    _load_env()
    return (
        os.environ.get("STRAVA_STATE_SECRET")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or "trixa-default-state"
    )


def sign_state(user_id: str) -> str:
    sig = hmac.new(_state_secret().encode(), user_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{user_id}:{sig}"


def verify_state(state: str) -> str | None:
    if not state or ":" not in state:
        return None
    uid, sig = state.rsplit(":", 1)
    expected = hmac.new(_state_secret().encode(), uid.encode(), hashlib.sha256).hexdigest()[:16]
    return uid if hmac.compare_digest(sig, expected) else None


def authorize_url(redirect_uri: str, state: str) -> str:
    cid, _ = _creds()
    return f"{STRAVA_AUTH_URL}?" + urlencode({
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "approval_prompt": "auto", "scope": "activity:read_all", "state": state,
    })


def exchange_code(code: str, redirect_uri: str) -> dict:
    cid, secret = _creds()
    r = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "code": code,
        "grant_type": "authorization_code",
    }, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def refresh_access_token(refresh_token: str) -> dict:
    cid, secret = _creds()
    r = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": cid, "client_secret": secret, "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- Token-lagring (public.strava_tokens) ----------


def get_tokens(client, user_id: str) -> dict | None:
    res = client.table("strava_tokens").select("*").eq("user_id", user_id).limit(1).execute()
    return res.data[0] if res.data else None


def save_tokens(client, user_id: str, access: str, refresh: str, expires_at: int,
                athlete_id: int | None, scope: str | None = None) -> None:
    client.table("strava_tokens").upsert({
        "user_id": user_id, "athlete_id": athlete_id, "access_token": access,
        "refresh_token": refresh, "expires_at": expires_at, "scope": scope,
    }, on_conflict="user_id").execute()


def delete_tokens(client, user_id: str) -> None:
    client.table("strava_tokens").delete().eq("user_id", user_id).execute()


def _ensure_fresh(client, user_id: str, tokens: dict) -> dict:
    if (tokens.get("expires_at") or 0) < time.time() + 60:
        ref = refresh_access_token(tokens["refresh_token"])
        save_tokens(client, user_id, ref["access_token"], ref["refresh_token"],
                    ref["expires_at"], tokens.get("athlete_id"), tokens.get("scope"))
        tokens = {**tokens, "access_token": ref["access_token"],
                  "refresh_token": ref["refresh_token"], "expires_at": ref["expires_at"]}
    return tokens


# ---------- Aktiviteter (public.strava_activities) ----------


def get_activities(access_token: str, after: int | None = None,
                   per_page: int = 200, max_pages: int = 3) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"per_page": per_page, "page": page}
        if after:
            params["after"] = after
        r = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params, timeout=_TIMEOUT,
        )
        if r.status_code == 429:
            break  # rate limited
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < per_page:
            break
    return out


def parse_activity(raw: dict) -> dict:
    sport = raw.get("type", raw.get("sport_type", "Unknown"))
    dist_m = raw.get("distance", 0) or 0
    moving = raw.get("moving_time", 0) or 0
    pace = None
    if sport in ("Run", "TrailRun") and dist_m and moving:
        s = moving / (dist_m / 1000)
        pace = f"{int(s // 60)}:{int(s % 60):02d}/km"
    elif sport in ("Swim", "OpenWaterSwim") and dist_m and moving:
        s = moving / (dist_m / 100)
        pace = f"{int(s // 60)}:{int(s % 60):02d}/100m"
    hr = raw.get("average_heartrate")
    watts = raw.get("average_watts")
    elev = raw.get("total_elevation_gain")
    return {
        "strava_id": int(raw["id"]),
        "date": (raw.get("start_date_local", "") or "")[:10],
        "type": SPORT_MAP.get(sport, sport),
        "name": raw.get("name", ""),
        "duration_min": round(moving / 60, 1) if moving else None,
        "distance_km": round(dist_m / 1000, 2) if dist_m else None,
        "avg_hr": int(hr) if hr else None,
        "avg_power": int(watts) if watts else None,
        "elevation_m": round(elev, 1) if elev else None,
        "pace": pace,
    }


def upsert_activities(client, user_id: str, raw_activities: list[dict]) -> int:
    rows = []
    for raw in raw_activities:
        try:
            a = parse_activity(raw)
        except (KeyError, ValueError, TypeError):
            continue
        if not a.get("date"):
            continue
        a["user_id"] = user_id
        rows.append(a)
    if rows:
        client.table("strava_activities").upsert(rows, on_conflict="strava_id").execute()
    return len(rows)


def sync_recent(client, user_id: str, days: int = 45) -> int:
    """Hämta + spara senaste `days` dagars Strava-aktiviteter. Returnerar antal."""
    tokens = get_tokens(client, user_id)
    if not tokens:
        return 0
    tokens = _ensure_fresh(client, user_id, tokens)
    after = int(time.time()) - days * 86400
    acts = get_activities(tokens["access_token"], after=after)
    return upsert_activities(client, user_id, acts)
