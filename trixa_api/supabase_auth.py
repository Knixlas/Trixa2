"""Supabase Auth (server-side) för Trixa-UI:t.

Adept-inloggning via Supabase GoTrue REST. Alla anrop görs från backend:
- apikey = projektnyckel (anon om satt, annars service-role — lämnar aldrig servern)
- användarens egen access_token (JWT) verifieras mot /auth/v1/user

Vi lagrar inga lösenord. Sessionen bärs av HttpOnly-cookies (access + refresh)
som sätts/läses i ui.py + middleware i main.py.
"""

from __future__ import annotations

import os

import requests

from coach.trixa.db import _load_env

_TIMEOUT = 15


def _base() -> tuple[str, str]:
    _load_env()
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = (
        os.environ.get("SUPABASE_ANON_KEY")
        or os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    )
    if not url or not key:
        raise RuntimeError("Saknar SUPABASE_URL och/eller nyckel för auth.")
    return url, key


def _session_from(payload: dict) -> dict:
    return {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "user_id": (payload.get("user") or {}).get("id"),
    }


def sign_in_password(email: str, password: str) -> dict | None:
    """Logga in med e-post + lösenord. Returnerar session-dict eller None."""
    url, key = _base()
    try:
        r = requests.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return _session_from(r.json())


def sign_up(email: str, password: str, name: str | None = None) -> tuple[dict | None, str | None]:
    """Skapa konto + logga in. Returnerar (session, fel-sträng).

    Använder admin-create med email_confirm=True → kontot är förbekräftat så
    ingen e-postbekräftelse/SMTP behövs, och vännen är inloggad direkt.
    handle_new_user-triggern skapar profil-raden (id, name, email).
    """
    url, key = _base()
    body: dict = {"email": email, "password": password, "email_confirm": True}
    if name:
        body["user_metadata"] = {"name": name}
    try:
        r = requests.post(
            f"{url}/auth/v1/admin/users",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None, "Kunde inte nå inloggningstjänsten — försök igen."
    if r.status_code in (200, 201):
        session = sign_in_password(email, password)
        if session and session.get("user_id"):
            return session, None
        return None, "Kontot skapades men inloggningen misslyckades — prova logga in."
    msg = ""
    try:
        j = r.json() or {}
        msg = str(j.get("msg") or j.get("error_description") or j.get("error") or "")
    except ValueError:
        pass
    low = msg.lower()
    if r.status_code in (409, 422) or any(k in low for k in ("already", "registered", "exists")):
        return None, "Det finns redan ett konto med den e-posten — logga in i stället."
    return None, "Kunde inte skapa kontot. Kontrollera uppgifterna och försök igen."


def get_user_id(access_token: str) -> str | None:
    """Verifiera en access_token mot Supabase och returnera user-id, annars None."""
    if not access_token:
        return None
    url, key = _base()
    try:
        r = requests.get(
            f"{url}/auth/v1/user",
            headers={"apikey": key, "Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return (r.json() or {}).get("id")


def refresh_session(refresh_token: str) -> dict | None:
    """Förnya en utgången access_token. Returnerar ny session-dict eller None."""
    if not refresh_token:
        return None
    url, key = _base()
    try:
        r = requests.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return _session_from(r.json())
