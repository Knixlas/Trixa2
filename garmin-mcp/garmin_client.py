"""
GarminClient - tunn wrapper runt python-garminconnect med token-caching
och hantering av aterinloggning + MFA.

Garminconnect 0.3.x anvander en intern "di_token" + "di_refresh_token"
istallet for de gamla oauth1/oauth2-tokenfilerna. Vi sparar dessa som
en enkel JSON och aterstaller dem genom direkt attribut-tilldelning
pa Garmin-objektets klient.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
)

logger = logging.getLogger("garmin-mcp.client")

TOKEN_FILE = "garmin_tokens.json"


class GarminClient:
    def __init__(self, email: str | None, password: str | None, token_dir: Path):
        if not email or not password:
            raise ValueError(
                "GARMIN_EMAIL och GARMIN_PASSWORD maste finnas i miljon (.env)."
            )
        self._email = email
        self._password = password
        self._token_dir = token_dir
        self._token_path = token_dir / TOKEN_FILE
        self._api: Garmin | None = None

    @property
    def api(self) -> Garmin:
        if self._api is None:
            self._api = self._login()
        return self._api

    def _login(self) -> Garmin:
        # 1. Forsok ateranvanda cachade tokens
        api = self._try_cached_login()
        if api is not None:
            return api

        # 2. Full login via email + losen (+ MFA)
        logger.info("Kor full inloggning mot Garmin")
        api = Garmin(email=self._email, password=self._password, return_on_mfa=True)
        result1, result2 = api.login()
        if result1 == "needs_mfa":
            mfa_code = input("MFA-kod fran Garmin (mail/SMS): ").strip()
            api.resume_login(result2, mfa_code)

        self._dump_tokens(api)
        return api

    def _try_cached_login(self) -> Garmin | None:
        """Forsok ateranvanda sparade tokens. Returnerar None om de saknas/ar ogiltiga."""
        if not self._token_path.exists():
            logger.info("Inga cachade tokens (%s saknas)", self._token_path)
            return None

        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            di_token = data.get("di_token")
            di_refresh = data.get("di_refresh_token")
            if not di_token or not di_refresh:
                logger.warning("Cachad tokens-fil saknar di_token/di_refresh_token")
                return None

            api = Garmin(email=self._email, password=self._password)
            # Stoppa in tokens direkt - hoppa over inloggning
            api.client.di_token = di_token
            api.client.di_refresh_token = di_refresh
            api.client.is_authenticated = True

            # Verifiera med ett latt API-anrop
            api.get_user_profile()
            logger.info("Inloggad via cachade tokens")
            return api
        except (GarminConnectAuthenticationError, json.JSONDecodeError, Exception) as e:
            logger.info("Cachade tokens ogiltiga (%s) - kor full login", type(e).__name__)
            return None

    def _dump_tokens(self, api: Garmin) -> None:
        """Spara di_token + di_refresh_token till en JSON-fil."""
        client = api.client
        di_token = getattr(client, "di_token", None)
        di_refresh = getattr(client, "di_refresh_token", None)

        if not di_token or not di_refresh:
            raise RuntimeError(
                "api.client saknar di_token/di_refresh_token efter login - "
                "garminconnect-API:t kan ha andrats. Kor diagnose_tokens.py."
            )

        self._token_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "di_token": di_token,
            "di_refresh_token": di_refresh,
        }
        self._token_path.write_text(json.dumps(payload), encoding="utf-8")
        size = self._token_path.stat().st_size
        logger.info("Tokens sparade till %s (%d bytes)", self._token_path, size)

    def refresh(self) -> None:
        """Tvinga ny inloggning."""
        self._api = None
        _ = self.api
