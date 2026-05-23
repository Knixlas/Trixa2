"""
GarminClient – tunn wrapper runt python-garminconnect med token-caching
och hantering av återinloggning + MFA.
"""
from __future__ import annotations

import logging
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
import garth
from garth.exc import GarthHTTPError

logger = logging.getLogger("garmin-mcp.client")


class GarminClient:
    """
    Lazy-loadande Garmin-klient. Försöker först återanvända sparade OAuth-tokens
    från `token_dir`; först om de saknas/är ogiltiga görs en full login med
    email + lösenord (och eventuell MFA-kod).
    """

    def __init__(self, email: str | None, password: str | None, token_dir: Path):
        if not email or not password:
            raise ValueError(
                "GARMIN_EMAIL och GARMIN_PASSWORD måste finnas i miljön (.env)."
            )
        self._email = email
        self._password = password
        self._token_dir = token_dir
        self._api: Garmin | None = None

    # ------------------------------------------------------------------
    @property
    def api(self) -> Garmin:
        if self._api is None:
            self._api = self._login()
        return self._api

    # ------------------------------------------------------------------
    def _login(self) -> Garmin:
        token_dir = str(self._token_dir)
        try:
            logger.info("Försöker återanvända sparade tokens från %s", token_dir)
            api = Garmin()
            api.login(token_dir)
            logger.info("Inloggad via cachad token.")
            return api
        except (FileNotFoundError, GarthHTTPError, GarminConnectAuthenticationError):
            logger.info("Ingen giltig cachad token – kör full inloggning.")

        api = Garmin(email=self._email, password=self._password, return_on_mfa=True)
        result1, result2 = api.login()

        if result1 == "needs_mfa":
            mfa_code = input("MFA-kod från Garmin (mail/SMS): ").strip()
            api.resume_login(result2, mfa_code)

        self._token_dir.mkdir(parents=True, exist_ok=True)
        garth.client.dump(token_dir)
        logger.info("Tokens sparade till %s", token_dir)
        return api

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Tvinga ny inloggning (anrop om Garmin börjar returnera 401)."""
        self._api = None
        _ = self.api
