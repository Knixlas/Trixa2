"""Deterministisk TrainingPeaks-klient för Trixa2.

Port av JamsusMaximus/trainingpeaks-mcp:s HTTP-lager, men:
- **synkron** (`requests`, som resten av repot — strava_client, supabase_auth)
  i stället för async httpx, eftersom engine/planner är synkrona,
- **utan MCP-/LLM-beroenden** — ren kod enligt Trixa-principen,
- med en **credential-provider-söm** så att den headless-lagrade cookien
  (Supabase-tabell `tp_auth`, se docs/06 task 8) kan pluggas in utan att
  röra klienten.

Auth: `Production_tpAuth`-cookie växlas mot en kortlivad OAuth-token
(`GET /users/v3/token`, ~1h), som cachas och förnyas automatiskt. Bearer
används på alla efterföljande anrop.

Cookie-källa (prioordning i `default_cookie_provider`):
1. explicit `cookie=`-argument
2. injicerad `cookie_provider`-callable (t.ex. Supabase-backad)
3. env `TP_AUTH_COOKIE`

Endpoints och type-ids är bekräftade mot MCP-källkoden, se
`docs/06_TP_INTEGRATION_REBUILD.md` §4.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Callable

import requests

TP_API_BASE = "https://tpapi.trainingpeaks.com"
TOKEN_ENDPOINT = "/users/v3/token"
USER_ENDPOINT = "/users/v3/user"

DEFAULT_TIMEOUT = 30.0
MIN_REQUEST_INTERVAL = 0.15   # ≥150 ms mellan anrop (samma som MCP:n)
TOKEN_REFRESH_BUFFER = 60     # förnya token 60 s före utgång
TOKEN_EXCHANGE_RETRIES = 5    # TP:s /users/v3/token 500:ar sporadiskt (ibland flera sek) → retry
TOKEN_EXCHANGE_BACKOFF = 2.0  # sek, linjär backoff (0,2,4,6,8 → ~20s total marginal)

CookieProvider = Callable[[], "str | None"]


# ---------- Fel ----------


class TPError(Exception):
    """Bas för TrainingPeaks-fel."""


class TPAuthError(TPError):
    """Cookie/token ogiltig eller utgången — kräver ny capture."""


class TPNotFoundError(TPError):
    """Resurs saknas (404)."""


class TPRateLimitError(TPError):
    """För många anrop (429)."""


# ---------- Cookie-källa ----------

ENV_VAR_NAME = "TP_AUTH_COOKIE"


def valid_env_cookie(raw: str | None) -> str | None:
    """Returnera env-cookien om den ser rimlig ut, annars None.

    Skyddar mot en trasig env-variabel (t.ex. inklistrad kommandotext) som
    annars tyst tar precedens och 500:ar token-växlingen. En äkta
    `Production_tpAuth` är lång och saknar whitespace.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) < 50 or any(c.isspace() for c in raw):
        return None
    return raw


def default_cookie_provider() -> str | None:
    """Hämta cookien från env (headless-vägen). Trasig env-cookie ignoreras."""
    return valid_env_cookie(os.environ.get(ENV_VAR_NAME))


# ---------- Klient ----------


class TPClient:
    """Synkron HTTP-klient mot TrainingPeaks interna API.

    Args:
        cookie: explicit `Production_tpAuth`-värde (vinner över provider/env).
        cookie_provider: callable som returnerar cookien (för injektion).
        athlete_id: hoppa över upplösning om redan känt.
        timeout: per-request-timeout i sekunder.
    """

    def __init__(
        self,
        cookie: str | None = None,
        cookie_provider: CookieProvider | None = None,
        athlete_id: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._explicit_cookie = cookie.strip() if cookie else None
        self._cookie_provider = cookie_provider or default_cookie_provider
        self._athlete_id = athlete_id
        self.timeout = timeout

        self._session = requests.Session()
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_request = 0.0

    # ---- cookie/token ----

    def _get_cookie(self) -> str:
        cookie = self._explicit_cookie or self._cookie_provider()
        if not cookie:
            raise TPAuthError(
                "Ingen TrainingPeaks-cookie. Sätt env TP_AUTH_COOKIE eller "
                "injicera en cookie_provider. Se docs/06 §3."
            )
        return cookie

    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.time() < (
            self._token_expires_at - TOKEN_REFRESH_BUFFER
        )

    def _exchange_cookie_for_token(self) -> None:
        """Växla cookie → access_token.

        Retry på transienta 5xx — TP:s token-endpoint 500:ar sporadiskt
        (verifierat live 2026-06-07). 401/403 = ogiltig cookie → ingen retry.
        """
        url = f"{TP_API_BASE}{TOKEN_ENDPOINT}"
        headers = {
            "Cookie": f"Production_tpAuth={self._get_cookie()}",
            "Accept": "application/json",
        }
        last_status: int | None = None
        for attempt in range(TOKEN_EXCHANGE_RETRIES):
            self._throttle()
            try:
                resp = self._session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt < TOKEN_EXCHANGE_RETRIES - 1:
                    time.sleep(TOKEN_EXCHANGE_BACKOFF * (attempt + 1))
                    continue
                raise TPError(f"Nätverksfel vid token-växling: {e}") from e

            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token") if isinstance(data, dict) else None
                if not token or "access_token" not in token:
                    raise TPError("Oväntat token-svar (saknar access_token).")
                self._access_token = token["access_token"]
                self._token_expires_at = time.time() + int(token.get("expires_in", 3600))
                return

            if resp.status_code in (401, 403):
                raise TPAuthError(
                    "Cookie utgången/ogiltig. Capture en ny Production_tpAuth. "
                    "Se docs/07 (token-rotation)."
                )

            # transient 5xx → backa och försök igen
            last_status = resp.status_code
            if resp.status_code >= 500 and attempt < TOKEN_EXCHANGE_RETRIES - 1:
                time.sleep(TOKEN_EXCHANGE_BACKOFF * (attempt + 1))
                continue
            raise TPError(f"Token-växling misslyckades: HTTP {resp.status_code}")

        raise TPError(
            f"Token-växling misslyckades efter {TOKEN_EXCHANGE_RETRIES} försök "
            f"(senast HTTP {last_status})"
        )

    def _ensure_token(self) -> None:
        if not self._token_valid():
            self._exchange_cookie_for_token()

    # ---- request-motor ----

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json: Any = None,
        params: dict | None = None,
        _retry_on_401: bool = True,
    ) -> Any:
        """Autentiserat anrop. Returnerar parsad JSON (eller None vid 204)."""
        self._ensure_token()
        self._throttle()
        url = f"{TP_API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            resp = self._session.request(
                method, url, headers=headers, json=json, params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise TPError(f"Nätverksfel ({method} {endpoint}): {e}") from e

        # Token kan ha dött mitt i — rensa och försök en gång till.
        if resp.status_code == 401 and _retry_on_401:
            self._access_token = None
            self._token_expires_at = 0.0
            return self._request(
                method, endpoint, json=json, params=params, _retry_on_401=False
            )

        return self._handle(resp, method, endpoint)

    @staticmethod
    def _handle(resp: requests.Response, method: str, endpoint: str) -> Any:
        if resp.status_code in (200, 201):
            try:
                return resp.json()
            except ValueError:
                return None
        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            raise TPAuthError("Sessionen utgången. Capture ny cookie.")
        if resp.status_code == 403:
            raise TPAuthError("Åtkomst nekad (403). Kontrollera behörighet/cookie.")
        if resp.status_code == 404:
            raise TPNotFoundError(f"Resurs saknas: {method} {endpoint}")
        if resp.status_code == 429:
            raise TPRateLimitError("Rate limit (429). Vänta och försök igen.")
        raise TPError(f"API-fel {resp.status_code}: {method} {endpoint}")

    # ---- athlete-id ----

    def ensure_athlete_id(self) -> int:
        """Lös ut athlete-id (cachas). För personkonto = `personId`.

        Faller tillbaka på första posten i `athletes[]` om personId saknas.
        Coach-roster-upplösning behövs inte för Trixa (en adept).
        """
        if self._athlete_id is not None:
            return self._athlete_id

        data = self._request("GET", USER_ENDPOINT)
        user = data.get("user", data) if isinstance(data, dict) else {}
        athlete_id = user.get("personId")
        if not athlete_id:
            athletes = user.get("athletes") or []
            if athletes:
                athlete_id = athletes[0].get("athleteId")
        if not athlete_id:
            raise TPAuthError("Kunde inte lösa athlete-id från /users/v3/user.")
        self._athlete_id = int(athlete_id)
        return self._athlete_id

    # ---- läs: aktiviteter & pass ----

    def get_workouts(
        self, start: date, end: date,
    ) -> list[dict]:
        """Pass (planerade + genomförda) i datumintervall (≤90 dagar)."""
        aid = self.ensure_athlete_id()
        endpoint = f"/fitness/v6/athletes/{aid}/workouts/{start.isoformat()}/{end.isoformat()}"
        data = self._request("GET", endpoint)
        return data if isinstance(data, list) else []

    def get_workout(self, workout_id: int | str) -> dict | None:
        aid = self.ensure_athlete_id()
        data = self._request("GET", f"/fitness/v6/athletes/{aid}/workouts/{workout_id}")
        return data if isinstance(data, dict) else None

    # ---- läs: hälsometrik ----

    def get_metrics(self, start: date, end: date) -> list[dict]:
        """Consolidated timed metrics: hrv (60), pulse/RHR (5), sleep-h (6), …"""
        aid = self.ensure_athlete_id()
        endpoint = (
            f"/metrics/v3/athletes/{aid}/consolidatedtimedmetrics/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        data = self._request("GET", endpoint)
        if isinstance(data, list):
            return data
        return [data] if isinstance(data, dict) else []

    # ---- läs: PMC (CTL/ATL/TSB) ----

    def get_performance_data(
        self, start: date, end: date, atl_constant: int = 7, ctl_constant: int = 42,
    ) -> list[dict]:
        """Performance Management Chart per dag: {workoutDay, tssActual, ctl, atl, tsb}.

        ctl→chronic_load, atl→acute_load, atl/ctl→load_ratio (ACWR-proxy).
        """
        aid = self.ensure_athlete_id()
        endpoint = (
            f"/fitness/v1/athletes/{aid}/reporting/performancedata/"
            f"{start.isoformat()}/{end.isoformat()}"
        )
        body = {
            "atlConstant": atl_constant,
            "atlStart": 0,
            "ctlConstant": ctl_constant,
            "ctlStart": 0,
            "workoutTypes": [],
        }
        data = self._request("POST", endpoint, json=body)
        return data if isinstance(data, list) else []

    # ---- skriv: pass ----

    def create_workout(self, payload: dict) -> dict:
        """Skapa pass. `payload` byggs av workout_writer/mapping (se docs/06 §7)."""
        aid = self.ensure_athlete_id()
        payload = {**payload, "athleteId": aid}
        data = self._request("POST", f"/fitness/v6/athletes/{aid}/workouts", json=payload)
        if not isinstance(data, dict):
            raise TPError("Oväntat svar vid create_workout.")
        return data

    def get_workout_raw(self, workout_id: int | str) -> dict:
        """Hämta råpass för merge-then-PUT (TP kräver hela objektet vid update)."""
        wk = self.get_workout(workout_id)
        if wk is None:
            raise TPNotFoundError(f"Pass {workout_id} saknas.")
        return wk

    def update_workout(self, workout_id: int | str, full_payload: dict) -> dict:
        """PUT helt pass-objekt (hämta → merge → put görs av anroparen)."""
        aid = self.ensure_athlete_id()
        data = self._request(
            "PUT", f"/fitness/v6/athletes/{aid}/workouts/{workout_id}", json=full_payload
        )
        return data if isinstance(data, dict) else {"workoutId": workout_id}

    def delete_workout(self, workout_id: int | str) -> None:
        aid = self.ensure_athlete_id()
        self._request("DELETE", f"/fitness/v6/athletes/{aid}/workouts/{workout_id}")

    # ---- diagnostik (token-health, task 8) ----

    def verify(self) -> dict:
        """Snabb hälsokoll: går cookien att växla och nå /user? För token-health."""
        try:
            aid = self.ensure_athlete_id()
        except TPAuthError as e:
            return {"ok": False, "reason": "auth", "message": str(e)}
        except TPError as e:
            return {"ok": False, "reason": "error", "message": str(e)}
        return {"ok": True, "athlete_id": aid}

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "TPClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
