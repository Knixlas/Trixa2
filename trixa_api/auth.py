"""Bearer-token-auth för Trixa-API.

För MVP: en delad token i env-var TRIXA_API_TOKEN. Nils-tråden använder
samma token. När vi har riktig adept-auth (mobil-app) byts detta till
Supabase JWT-validering.
"""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request, status


def _expected_token() -> str | None:
    return os.environ.get("TRIXA_API_TOKEN")


def require_api_token(request: Request) -> None:
    """Kasta 401 om Authorization-header saknas eller är fel."""
    expected = _expected_token()
    if expected is None:
        # Ingen token konfigurerad — öppet API. Tillåt bara i lokal dev.
        if os.environ.get("TRIXA_ALLOW_NO_AUTH") != "1":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="TRIXA_API_TOKEN är inte konfigurerad och TRIXA_ALLOW_NO_AUTH är inte satt.",
            )
        return

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Saknar Bearer-token i Authorization-header.",
        )
    token = header[len("Bearer ") :].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ogiltig Bearer-token.",
        )
