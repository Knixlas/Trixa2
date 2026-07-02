"""Headless TrainingPeaks-inloggning → `Production_tpAuth`-cookie.

Låter en adept koppla sin egen TP med användarnamn + lösenord i Trixa-UI:t,
i stället för att manuellt capture:a en cookie ur webbläsaren. Vi loggar in
serverside, fångar **bara** session-cookien och lämnar den till
``auth_store.store_cookie`` (per user_id, RLS). **Lösenordet sparas aldrig.**

Flödet speglar TP:s webb-login:
1. ``GET https://home.trainingpeaks.com/login`` → HTML med ett dolt
   ``__RequestVerificationToken`` (CSRF) + sätter en temporär cookie.
2. ``POST`` samma URL med ``Username``/``Password``/token i en delad session.
3. Vid lyckad login sätter TP ``Production_tpAuth`` på ``.trainingpeaks.com`` —
   vi plockar den ur cookie-jaren.

⚠️ Skört med flit: TP exponerar ingen publik OAuth, så det här härmar webb-
formuläret. Ändrar TP login-sidan, slår på CAPTCHA eller kräver MFA så slutar
det fungera — då faller vi tillbaka på manuell cookie-capture (``auth_store``
CLI). Felen nedan är därför uttryckliga så UI:t kan vägleda användaren.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from .client import TPAuthError, TPError, valid_env_cookie

LOGIN_URL = "https://home.trainingpeaks.com/login"
AUTH_COOKIE_NAME = "Production_tpAuth"
DEFAULT_TIMEOUT = 30.0

# Dolt anti-forgery-fält i login-formuläret.
_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', re.IGNORECASE
)
# Tecken på att TP blockerar automatiserad login (captcha/MFA) — ger ett
# tydligare felmeddelande än "fel lösenord".
_CHALLENGE_RE = re.compile(r"captcha|recaptcha|two[\s-]?factor|verification code", re.IGNORECASE)


def _extract_token(html: str) -> str | None:
    m = _TOKEN_RE.search(html or "")
    return m.group(1) if m else None


def _find_auth_cookie(session: requests.Session) -> str | None:
    """Plocka Production_tpAuth ur jaren oavsett subdomän."""
    for c in session.cookies:
        if c.name == AUTH_COOKIE_NAME and c.value:
            return c.value.strip()
    return None


def login_and_get_cookie(
    username: str,
    password: str,
    *,
    session: requests.Session | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Logga in på TP och returnera en giltig ``Production_tpAuth``-cookie.

    Raises:
        TPAuthError: fel inloggningsuppgifter, eller CAPTCHA/MFA blockerar.
        TPError: nätverksfel eller oväntat svar (login-sidan ändrad).
    """
    if not username or not username.strip() or not password:
        raise TPAuthError("Användarnamn och lösenord krävs.")
    sess = session or requests.Session()
    sess.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (compatible; Trixa/1.0; +https://trixa.app)",
    )

    # 1) Hämta login-sidan → CSRF-token + initiala cookies.
    try:
        page = sess.get(LOGIN_URL, timeout=timeout)
    except requests.RequestException as e:
        raise TPError(f"Nätverksfel mot TrainingPeaks: {e}") from e
    if page.status_code != 200:
        raise TPError(f"Kunde inte nå TP login-sidan (HTTP {page.status_code}).")

    token = _extract_token(page.text)
    if not token:
        raise TPError(
            "Hittade inget login-formulär hos TrainingPeaks — sidan kan ha "
            "ändrats. Koppla via manuell cookie tills vidare."
        )

    # 2) Posta inloggningen i samma session (CSRF + cookies följer med).
    form = {
        "Username": username.strip(),
        "Password": password,
        "__RequestVerificationToken": token,
    }
    try:
        resp = sess.post(
            LOGIN_URL, data=form, timeout=timeout,
            headers={"Referer": LOGIN_URL},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        raise TPError(f"Nätverksfel vid TP-inloggning: {e}") from e

    # 3) Lyckad login → auth-cookien finns i jaren.
    cookie = _find_auth_cookie(sess)
    if cookie and valid_env_cookie(cookie):
        return cookie

    # Ingen cookie → diagnostisera varför.
    if _CHALLENGE_RE.search(resp.text or ""):
        raise TPAuthError(
            "TrainingPeaks kräver CAPTCHA/extra verifiering — automatisk "
            "inloggning går inte. Koppla via manuell cookie i stället."
        )
    raise TPAuthError(
        "Inloggningen nekades — kontrollera användarnamn och lösenord "
        "(eller koppla via manuell cookie om TP infört MFA)."
    )


def login_and_store(username: str, password: str, user_id: str, pg: Any = None) -> int:
    """Logga in och spara cookien för ``user_id`` i ``tp_auth``. Returnerar
    cookie-längden (för logg/feedback). Lösenordet lagras aldrig."""
    from .auth_store import store_cookie

    cookie = login_and_get_cookie(username, password)
    store_cookie(cookie, user_id, pg=pg)
    return len(cookie)
