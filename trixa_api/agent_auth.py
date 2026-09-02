"""Per-adept Bearer-token-auth för agent-API:t (``/agent/*``) och ``/mcp``.

En AI-klient får en token som **scope:ar alla anrop till en adept**. Token =
identitet: endpointen härleder adepten ur token, det finns ingen
``athlete_user_id``-parameter att manipulera (ingen IDOR mellan adepter).

**Två sorters token accepteras**, i den ordningen:

1. **Personlig token** (``trixa_…``) som adepten skapar i Inställningar och
   klistrar in i en klient som kan sätta egna headers (Claude Code, Cursor).
   Rad i ``public.api_tokens``.
2. **OAuth-access-token** (``trixa_at_…``) utfärdad av auktoriseringsservern i
   ``oauth_server`` för klienter som inte kan sätta headers (claude.ai). Rad i
   ``public.oauth_tokens``, med **audience** bunden till den här MCP-servern.

Säkerhet:
- Bara token-**hashen** (sha256 hex) lagras. Råvärdet visas en gång.
- Upplösning sker serverside via service-role (requesten har ingen Supabase-JWT).
- Återkallning: sätt ``revoked_at`` → token slutar matcha direkt. Gäller båda.
- 401-svaret pekar ut resursmetadatan (RFC 9728) så en OAuth-klient vet var
  auktoriseringsservern finns. Utan den pekaren kan den inte komma vidare.
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


def unauthorized_headers(request: Request) -> dict[str, str]:
    """``WWW-Authenticate`` för ett 401-svar, med pekare till resursmetadatan.

    RFC 9728 §5.1. Det här är startpunkten för hela OAuth-kedjan: utan
    ``resource_metadata`` har klienten ingenstans att leta efter
    auktoriseringsservern och faller tillbaka på att fråga användaren om ett
    Client ID som ingen kan svara på.
    """
    try:
        from trixa_api.oauth_server import public_base_url

        base = public_base_url(request)
    except Exception:  # noqa: BLE001 — headern får aldrig fälla 401-svaret
        return {"WWW-Authenticate": "Bearer"}
    metadata = f"{base}/.well-known/oauth-protected-resource"
    return {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}


def resolve_agent_scope(request: Request) -> AgentScope:
    """FastAPI-dependency: Bearer-token → AgentScope (adept-scope).

    Provar personlig token först, sedan OAuth-access-token. 401 om ingen
    matchar. Uppdaterar ``last_used_at`` best-effort (failar inte anropet om
    den skrivningen strular).
    """
    raw = _extract_bearer(request)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Saknar Bearer-token i Authorization-header.",
            headers=unauthorized_headers(request),
        )
    client = get_postgrest()
    try:
        res = (
            client.table("api_tokens")
            .select("id, user_id, name, revoked_at, last_used_at")
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
        oauth_scope = _resolve_via_oauth(request, raw)
        if oauth_scope is not None:
            return oauth_scope
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ogiltig eller återkallad token.",
            headers=unauthorized_headers(request),
        )
    row = rows[0]
    # last_used_at är en indikation ("använd nyligen"), inte en logg. En
    # UPDATE per verktygsanrop — claude.ai gör tre-fyra per tur — var en
    # radskrivning för ingenting (docs/12 H6). Skriv bara om det är >5 min
    # sedan sist.
    try:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        last = row.get("last_used_at")
        stale = True
        if last:
            try:
                stale = now - datetime.fromisoformat(str(last).replace("Z", "+00:00")) > timedelta(minutes=5)
            except ValueError:
                stale = True
        if stale:
            client.table("api_tokens").update(
                {"last_used_at": now.isoformat()}
            ).eq("id", row["id"]).execute()
    except Exception:  # noqa: BLE001 — best-effort, blockera aldrig anropet
        pass
    return AgentScope(user_id=row["user_id"], token_id=row["id"], name=row.get("name") or "")


def _resolve_via_oauth(request: Request, raw: str) -> AgentScope | None:
    """OAuth-access-token → AgentScope, eller None om den inte gäller här.

    Audience-kontrollen ligger i ``oauth_server.resolve_oauth_token``: en token
    utfärdad för någon annan resurs får inte fungera mot oss.
    """
    try:
        from trixa_api.oauth_server import canonical_resource, resolve_oauth_token

        row = resolve_oauth_token(raw, canonical_resource(request))
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return AgentScope(
        user_id=row["user_id"],
        token_id=str(row["id"]),
        name=row.get("client_id") or "oauth",
    )
