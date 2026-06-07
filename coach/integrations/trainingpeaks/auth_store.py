"""Headless cookie-lagring för TP — en cookie **per användare** (multi-tenant).

Tabell `public.tp_auth` (se migration ``tp_auth_multi_tenant``):

    id        bigint  (sekvens-backad)
    user_id   uuid    unique not null   -- ägaren (public.profiles.id)
    cookie    text    not null
    updated_at timestamptz

RLS: inloggade hanterar sin egen rad (``user_id = auth.uid()``); service-role
(backend/worker) kringgår RLS.

`client.TPClient` har en ``cookie_provider``-söm. ``supabase_cookie_provider(user_id)``
ger en provider som läser **just den användarens** cookie — så varje adept kan
koppla sin egen TrainingPeaks utan att koden rör övriga.

Lagra/rotera en cookie:
    Get-Content cookie.txt | python -m coach.integrations.trainingpeaks.auth_store --user <uuid>
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable


def store_cookie(cookie: str, user_id: str, pg: Any = None) -> None:
    """Spara/rotera en användares TP-cookie (upsert på user_id)."""
    if not cookie or not cookie.strip():
        raise ValueError("Tom cookie.")
    if not user_id:
        raise ValueError("user_id krävs — en cookie lagras per användare.")
    if pg is None:
        from ...trixa.db import get_postgrest
        pg = get_postgrest()
    pg.table("tp_auth").upsert(
        {"user_id": user_id, "cookie": cookie.strip()}, on_conflict="user_id"
    ).execute()


def supabase_cookie_provider(user_id: str, pg: Any = None) -> Callable[[], "str | None"]:
    """Provider som läser EN användares cookie ur ``public.tp_auth``.

    Per-user → multi-tenant. Misslyckas tyst (returnerar None) om raden saknas
    eller tabellen är onåbar; ``TPClient`` höjer då ``TPAuthError`` med åtgärd.

    Medvetet **ingen env-global** här — det vore en fälla i multi-tenant (alla
    användare skulle få samma cookie). Env-vägen finns kvar i
    ``client.default_cookie_provider`` för explicit en-användar/CI-bruk.
    """
    if not user_id:
        raise ValueError("supabase_cookie_provider kräver user_id (multi-tenant).")

    def _provider() -> str | None:
        client = pg
        try:
            if client is None:
                from ...trixa.db import get_postgrest
                client = get_postgrest()
            res = (
                client.table("tp_auth").select("cookie")
                .eq("user_id", user_id).limit(1).execute()
            )
            data = getattr(res, "data", None) or []
            if data and data[0].get("cookie"):
                return str(data[0]["cookie"]).strip()
        except Exception:  # noqa: BLE001 — saknad rad/nät → låt klienten rapportera
            return None
        return None

    return _provider


def main(argv: list[str] | None = None) -> int:
    """CLI: lagra/rotera en användares TP-cookie. Cookien läses från stdin
    (eller env ``TP_AUTH_COOKIE`` med ``--from-env``) — aldrig som argument,
    så den inte hamnar i shell-historik/loggar."""
    import argparse

    from .client import valid_env_cookie

    ap = argparse.ArgumentParser(description="Lagra TP-cookie för en användare.")
    ap.add_argument("--user", required=True, help="user_id (public.profiles.id)")
    ap.add_argument("--from-env", action="store_true",
                    help="läs cookien från env TP_AUTH_COOKIE i stället för stdin")
    args = ap.parse_args(argv)

    cookie = (os.environ.get("TP_AUTH_COOKIE", "") if args.from_env else sys.stdin.read())
    cookie = (cookie or "").strip()
    if not valid_env_cookie(cookie):
        print("FEL: cookien ser inte giltig ut (för kort eller innehåller whitespace).")
        return 1
    store_cookie(cookie, args.user)
    print(f"OK: TP-cookie lagrad för {args.user} ({len(cookie)} tecken).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
