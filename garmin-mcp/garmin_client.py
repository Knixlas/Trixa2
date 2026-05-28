"""
GarminClient - tunn wrapper runt python-garminconnect med token-caching
och hantering av aterinloggning + MFA.

Garminconnect 0.3.x anvander en intern "di_token" + "di_refresh_token"
istallet for de gamla oauth1/oauth2-tokenfilerna.

Token-storage:
- Primar: Supabase garmin_coach.oauth_tokens (om supabase-klient given)
- Sekundar/backup: fil garmin_tokens.json i token_dir
- Vid load: las Supabase forst, fall back till fil
- Vid save: skriv till BAGGE for redundans

Detta loser problemet med single-use refresh tokens — workflow:n kan
lasa OCH skriva tokens med samma SUPABASE_SERVICE_ROLE_KEY den redan har.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
)

logger = logging.getLogger("garmin-mcp.client")

TOKEN_FILE = "garmin_tokens.json"

# I CI (GitHub Actions) finns ingen TTY for MFA-prompt.
# Da maste vi misslyckas tydligt istallet for att hanga pa input().
IS_NON_INTERACTIVE = (
    not sys.stdin.isatty()
    or os.environ.get("CI") == "true"
    or os.environ.get("GITHUB_ACTIONS") == "true"
)


class GarminClient:
    def __init__(
        self,
        email: str | None,
        password: str | None,
        token_dir: Path,
        supabase_client: Any | None = None,
    ):
        if not email or not password:
            raise ValueError(
                "GARMIN_EMAIL och GARMIN_PASSWORD maste finnas i miljon (.env)."
            )
        self._email = email
        self._password = password
        self._token_dir = token_dir
        self._token_path = token_dir / TOKEN_FILE
        self._supabase = supabase_client
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

        # 2. Full login - krar TTY for MFA, sa misslyckas tydligt i CI
        if IS_NON_INTERACTIVE:
            raise RuntimeError(
                "Cachade tokens funkar inte och vi ar i CI utan TTY. "
                "Kor 'python test_connection.py' lokalt for att skapa nya tokens, "
                "kor sedan setup_github_secrets.ps1 for att uppdatera GARMIN_TOKENS_JSON."
            )

        logger.info("Kor full inloggning mot Garmin")
        api = Garmin(email=self._email, password=self._password, return_on_mfa=True)
        result1, result2 = api.login()
        if result1 == "needs_mfa":
            mfa_code = input("MFA-kod fran Garmin (mail/SMS): ").strip()
            api.resume_login(result2, mfa_code)

        self._dump_tokens(api)
        return api

    def _load_tokens_from_supabase(self) -> tuple[str, str] | None:
        """Las tokens fran garmin_coach.oauth_tokens. Returnerar (di_token, di_refresh)."""
        if self._supabase is None:
            print("[token-store] Supabase-klient saknas — hoppar over", flush=True)
            return None
        try:
            res = (
                self._supabase.schema("garmin_coach")
                .table("oauth_tokens")
                .select("di_token, di_refresh_token, updated_at")
                .eq("email", self._email)
                .execute()
            )
        except Exception as e:  # noqa: BLE001
            print(f"[token-store] Supabase-lasning failade: {type(e).__name__}: {e}", flush=True)
            return None
        if not res.data:
            print(f"[token-store] Supabase: ingen rad for email={self._email}", flush=True)
            return None
        row = res.data[0]
        if not row.get("di_token") or not row.get("di_refresh_token"):
            print("[token-store] Supabase: rad finns men tokens ar None", flush=True)
            return None
        print(
            f"[token-store] Supabase: tokens lasta (uppdaterade {row.get('updated_at')}, "
            f"di_token={len(row['di_token'])} chars, refresh={len(row['di_refresh_token'])} chars)",
            flush=True,
        )
        return row["di_token"], row["di_refresh_token"]

    def _load_tokens_from_file(self) -> tuple[str, str] | None:
        """Las tokens fran filsystemet. Fall back om Supabase saknas/tom."""
        if not self._token_path.exists():
            logger.info("Inga cachade tokens i fil (%s saknas)", self._token_path)
            return None
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning("Cachad tokens-fil ar inte giltig JSON: %s", e)
            return None
        di_token = data.get("di_token")
        di_refresh = data.get("di_refresh_token")
        if not di_token or not di_refresh:
            logger.warning("Cachad tokens-fil saknar di_token/di_refresh_token")
            return None
        return di_token, di_refresh

    def _try_cached_login(self) -> Garmin | None:
        """Forsok ateranvanda sparade tokens. Returnerar None om de saknas/ar ogiltiga.

        Las fran Supabase forst (primar storage), fall back till fil.
        """
        tokens = self._load_tokens_from_supabase()
        source = "supabase"
        if tokens is None:
            tokens = self._load_tokens_from_file()
            source = "fil"
        if tokens is None:
            print("[token-store] BADE Supabase och fil tomma — kor full login", flush=True)
            return None
        di_token, di_refresh = tokens
        print(f"[token-store] Anvander tokens fran: {source}", flush=True)

        # Initiera Garmin-objektet utan att direkt logga in.
        # api.client skapas i __init__, sa vi kan stoppa in tokens dar.
        try:
            api = Garmin(email=self._email, password=self._password)
            # api.client ar en garminconnect.Client som har di_token-attributen.
            # is_authenticated ar en read-only property som hardleds fran di_token,
            # sa den blir True automatiskt nar vi satter tokens.
            client = api.client
            client.di_token = di_token
            client.di_refresh_token = di_refresh

            # Verifiera tokens med ett latt API-anrop. garminconnect kan
            # refresha tokens internt under detta anrop (Garmin anvander
            # single-use refresh tokens), sa vi maste spara tillbaka direkt.
            api.get_user_profile()
            print(f"[token-store] Tokens VERIFIERADE (kalla: {source})", flush=True)
            logger.info("Inloggad via cachade tokens (kalla: %s)", source)
            try:
                self._dump_tokens(api)
            except Exception as e:  # noqa: BLE001
                logger.warning("Kunde inte spara refreshade tokens: %s", e)
            return api
        except Exception as e:
            print(
                f"[token-store] Tokens AVVISADE av Garmin (kalla: {source}): "
                f"{type(e).__name__}: {str(e)[:200]}",
                flush=True,
            )
            logger.info("Cachade tokens ogiltiga (%s: %s) - kor full login",
                        type(e).__name__, e)
            return None

    def _dump_tokens(self, api: Garmin) -> None:
        """Spara di_token + di_refresh_token till BAGGE Supabase och fil.

        Supabase = primary store (overlever GitHub Actions runner-cleanup).
        Fil = backup om Supabase ar onaabar.
        """
        client = api.client
        di_token = getattr(client, "di_token", None)
        di_refresh = getattr(client, "di_refresh_token", None)

        if not di_token or not di_refresh:
            raise RuntimeError(
                "api.client saknar di_token/di_refresh_token efter login - "
                "garminconnect-API:t kan ha andrats."
            )

        # 1. Skriv till Supabase (primary)
        if self._supabase is not None:
            try:
                self._supabase.schema("garmin_coach").table("oauth_tokens").upsert(
                    {
                        "email": self._email,
                        "di_token": di_token,
                        "di_refresh_token": di_refresh,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="email",
                ).execute()
                logger.info("Tokens sparade till Supabase (email=%s)", self._email)
            except Exception as e:  # noqa: BLE001
                logger.warning("Kunde inte skriva tokens till Supabase: %s", e)

        # 2. Skriv till fil (backup)
        try:
            self._token_dir.mkdir(parents=True, exist_ok=True)
            payload = {"di_token": di_token, "di_refresh_token": di_refresh}
            self._token_path.write_text(json.dumps(payload), encoding="utf-8")
            size = self._token_path.stat().st_size
            logger.info("Tokens sparade till fil %s (%d bytes)", self._token_path, size)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kunde inte skriva tokens till fil: %s", e)

    def refresh(self) -> None:
        """Tvinga ny inloggning."""
        self._api = None
        _ = self.api

    def save_tokens(self) -> None:
        """Spara aktuella tokens till disk. Anropas efter sync sa
        eventuellt refreshade tokens inte kastas vid program-exit.

        Garmin anvander single-use refresh tokens — om vi inte sparar
        tillbaka efter varje sync invalideras refresh_token vid nasta
        kornings forsta API-anrop.
        """
        if self._api is None:
            return
        try:
            self._dump_tokens(self._api)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kunde inte spara tokens efter sync: %s", e)
