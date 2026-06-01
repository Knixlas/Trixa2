"""Supabase-klient för Trixa-skiktet — via postgrest direkt.

Vi använder postgrest istället för supabase-metapackaget för att slippa
pyiceberg-deppen som inte bygger mot Python 3.14. För Trixa räcker det
— vi behöver CRUD mot tabeller, inte realtime/auth/storage.

Service-role-key används eftersom planner kör som backend-process och
måste kunna skriva oavsett RLS. För adept-context (RLS aktiv) — använd
istället `get_postgrest_anon()`.

Använd:
    from coach.trixa.db import get_postgrest
    client = get_postgrest()
    client.from_("athlete_profiles").select("*").eq("user_id", uid).execute()
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from postgrest import SyncPostgrestClient


# Letar efter .env i Trixa2-roten (eller ovanför, för OneDrive-fallet)
_ENV_CANDIDATES = [
    Path(__file__).resolve().parent.parent.parent / ".env",
    Path(__file__).resolve().parent.parent.parent.parent / ".env",
]


def _load_env() -> None:
    """Läs .env om paketet python-dotenv finns och .env existerar."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in _ENV_CANDIDATES:
        if candidate.exists():
            load_dotenv(candidate)
            return


class SupabaseConfigError(RuntimeError):
    """Supabase-credentials saknas eller är felaktiga."""


def _build_url(base: str) -> str:
    """https://xxx.supabase.co  →  https://xxx.supabase.co/rest/v1"""
    base = base.rstrip("/")
    if base.endswith("/rest/v1"):
        return base
    return f"{base}/rest/v1"


@lru_cache(maxsize=1)
def get_postgrest() -> SyncPostgrestClient:
    """Postgrest-klient med service-role-key. För backend / planner."""
    _load_env()
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    if not url or not key:
        raise SupabaseConfigError(
            "Saknar SUPABASE_URL och/eller SUPABASE_SERVICE_KEY. "
            f"Letat efter .env i: {[str(p) for p in _ENV_CANDIDATES]}"
        )
    return SyncPostgrestClient(
        _build_url(url),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
        },
    )


@lru_cache(maxsize=1)
def get_postgrest_anon() -> SyncPostgrestClient:
    """Postgrest-klient med anon-key — RLS gäller. För adept-anrop."""
    _load_env()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        raise SupabaseConfigError(
            "Saknar SUPABASE_URL och/eller SUPABASE_ANON_KEY för anon-klient."
        )
    return SyncPostgrestClient(
        _build_url(url),
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
        },
    )


# Bakåtkompatibel alias — planner.py importerar `get_supabase`.
get_supabase = get_postgrest
get_supabase_anon = get_postgrest_anon
