"""Headless cookie-lagring för TP (Supabase `public.tp_auth`, en rad).

`client.TPClient` har en `cookie_provider`-söm. Den här modulen ger en
Supabase-backad provider för Railway-workern: cookien delas mellan web + worker
och kan roteras utan redeploy. Env `TP_AUTH_COOKIE` vinner fortfarande (CI/lokalt).

Tabell (skapas vid go-live, se docs/07_TP_SYNC_RUNBOOK.md):

    create table public.tp_auth (
      id int primary key default 1,
      cookie text not null,
      updated_at timestamptz default now(),
      check (id = 1)            -- garantera en rad
    );

Cookien är en känslig session-credential — lagras i Supabase (service-role-
skyddad), aldrig i klartext i loggar.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .client import ENV_VAR_NAME


def store_cookie(cookie: str, pg: Any = None) -> None:
    """Spara/rotera TP-cookien i Supabase. Kör efter en ny browser-capture."""
    if not cookie or not cookie.strip():
        raise ValueError("Tom cookie.")
    if pg is None:
        from ...trixa.db import get_postgrest
        pg = get_postgrest()
    pg.from_("tp_auth").upsert({"id": 1, "cookie": cookie.strip()}, on_conflict="id").execute()


def supabase_cookie_provider(pg: Any = None) -> Callable[[], "str | None"]:
    """Skapa en provider som läser cookien från env först, annars Supabase.

    Skicka resultatet till `TPClient(cookie_provider=...)`. Misslyckas tyst
    (returnerar None) om tabellen saknas eller är tom — klienten höjer då
    TPAuthError med tydlig åtgärd.
    """
    def _provider() -> str | None:
        env = os.environ.get(ENV_VAR_NAME)
        if env:
            return env.strip()
        try:
            client = pg
            if client is None:
                from ...trixa.db import get_postgrest
                client = get_postgrest()
            res = client.from_("tp_auth").select("cookie").eq("id", 1).limit(1).execute()
            data = getattr(res, "data", None) or []
            if data and data[0].get("cookie"):
                return str(data[0]["cookie"]).strip()
        except Exception:  # noqa: BLE001 — saknad tabell/nät → låt klienten rapportera
            return None
        return None

    return _provider
