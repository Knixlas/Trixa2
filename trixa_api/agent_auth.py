"""Per-adept Bearer-token-auth för agent-API:t (``/agent/*``).

En extern AI (Nils, Maja, …) får en token som **scope:ar alla anrop till en
adept**. Token = identitet: endpointen härleder adepten ur token, det finns
ingen ``athlete_user_id``-parameter att manipulera (ingen IDOR mellan adepter).

Säkerhet:
- Bara token-**hashen** (sha256 hex) lagras i ``public.api_tokens``. Råvärdet
  visas en gång vid skapande och kan aldrig läsas igen.
- Upplösning sker serverside via service-role (agent-requesten har ingen
  Supabase-JWT — bara sin Bearer-token).
- Återkallning: sätt ``revoked_at`` → token slutar matcha direkt.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from coach.trixa.db import get_postgrest

TOKEN_PREFIX = "trixa_"
_RAW_BYTES = 32  # → 43 url-safe base64-tecken


@dataclass(frozen=True)
class AgentScope:
    """Den upplösta adepten + token-metadata för ett autentiserat agent-anrop."""

    user_id: str
    token_id: str
    name: str


def hash_token(raw: str) -> str:
    """sha256-hex av råtoken (det vi jämför/lagrar)."""
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Skapa en ny token. Returnerar (raw, token_hash, token_prefix).

    ``raw`` visas för användaren EN gång; bara hash + prefix lagras.
    """
    raw = TOKEN_PREFIX + secrets.token_urlsafe(_RAW_BYTES)
    # Prefix för visning i listan: "trixa_" + 6 tecken (ej hemligt).
    prefix = raw[: len(TOKEN_PREFIX) + 6]
    return raw, hash_token(raw), prefix


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    return token or None


def resolve_agent_scope(request: Request) -> AgentScope:
    """FastAPI-dependency: Bearer-token → AgentScope (adept-scope).

    401 om token saknas/ogiltig/återkallad. Uppdaterar ``last_used_at``
    best-effort (failar inte anropet om den skrivningen strular).
    """
    raw = _extract_bearer(request)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Saknar Bearer-token i Authorization-header.",
        )
    client = get_postgrest()
    try:
        res = (
            client.table("api_tokens")
            .select("id, user_id, name, revoked_at")
            .eq("token_hash", hash_token(raw))
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Token-uppslag misslyckades: {exc}",
        )
    rows = res.data or []
    if not rows or rows[0].get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ogiltig eller återkallad token.",
        )
    row = rows[0]
    try:
        from datetime import datetime, timezone

        client.table("api_tokens").update(
            {"last_used_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", row["id"]).execute()
    except Exception:  # noqa: BLE001 — best-effort, blockera aldrig anropet
        pass
    return AgentScope(user_id=row["user_id"], token_id=row["id"], name=row.get("name") or "")
