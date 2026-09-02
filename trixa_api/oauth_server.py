"""OAuth 2.1-auktoriseringsserver framför ``/mcp``.

**Varför den finns:** AI-klienter som claude.ai (webb och mobil) kan inte sätta
en egen ``Authorization``-header i sin connector-dialog — de förhandlar OAuth.
Utan den här servern kan bara klienter som läser en config-fil (Claude Code,
Claude Desktop, Cursor) koppla upp sig mot Trixa.

**Kedjan klienten går igenom**, enligt MCP:s auktoriseringsspec::

    1. POST /mcp utan token          → 401 + WWW-Authenticate: resource_metadata=...
    2. GET  /.well-known/oauth-protected-resource   (RFC 9728) → var ligger AS:en?
    3. GET  /.well-known/oauth-authorization-server (RFC 8414) → vilka endpoints?
    4. POST /oauth/register          (RFC 7591) → client_id, utan handpåläggning
    5. GET  /oauth/authorize         → adepten loggar in och godkänner
    6. POST /oauth/token             → access + refresh
    7. POST /mcp med Bearer          → verktygen

Steg 1 är det som brukar fälla integrationer: utan ``resource_metadata`` i
401-svaret har klienten ingenstans att börja leta och faller tillbaka på att
fråga användaren om ett Client ID som ingen kan svara på.

**Säkerhetsval, och varför:**

- **PKCE S256 obligatoriskt.** ``code_challenge_methods_supported`` måste finnas
  i AS-metadatan — saknas den vägrar klienter ansluta även om allt annat stämmer.
- **Public clients, ingen client_secret.** Claudes connector kan inte hålla på en
  hemlighet. PKCE är det som binder koden till den som begärde den.
- **Exakt redirect-matchning** mot registrerade värden. Att spegla tillbaka det
  ``redirect_uri`` som kom in vore en öppen redirect.
- **Opaka tokens, bara hashar i DB.** Samma modell som ``api_tokens``. Ingen
  signeringsnyckel att rotera, och återkallning verkar omedelbart.
- **Audience-bindning (RFC 8707).** Token bär vilken resurs den gäller;
  ``/mcp`` avvisar tokens utfärdade för något annat.
- **Rotation med familjeåterkallning.** Återanvänd refresh-token eller
  återanvänd kod → hela kedjan för (klient, adept) återkallas.

Personliga ``trixa_``-tokens i ``api_tokens`` fungerar parallellt och orört.

Setup och felsökning: ``docs/11_MCP_CONNECTOR.md``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from coach.trixa.db import get_postgrest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

# Livslängder. Korta access-tokens begränsar skadan av en läckt token; refresh
# roteras vid varje användning så en stulen refresh upptäcks när originalet
# används igen.
CODE_TTL_SECONDS = 60
ACCESS_TTL_SECONDS = 60 * 60
REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30

# Öppen registrering är vad MCP-specen rekommenderar, men en trasig klient i
# omstartsloop ska inte kunna fylla tabellen.
MAX_REGISTRATIONS_PER_HOUR = 200
MAX_REDIRECT_URIS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def public_base_url(request: Request) -> str:
    """Adressen klienter ska prata med — utan avslutande snedstreck.

    Bakom Railways proxy stämmer inte alltid ``request.base_url`` (schemat blir
    http internt), och OAuth-metadata med fel scheme får klienten att vägra.
    TRIXA_PUBLIC_URL vinner när den är satt.
    """
    configured = os.environ.get("TRIXA_PUBLIC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return urlunparse((scheme, host, "", "", "", "")).rstrip("/")


def canonical_resource(request: Request) -> str:
    """Kanonisk URI för MCP-servern — det tokens audience binds till."""
    return f"{public_base_url(request)}/mcp"


def acceptable_resources(request: Request) -> set[str]:
    """``resource`` klienten får skicka. Specen tillåter både med och utan path."""
    base = public_base_url(request)
    return {f"{base}/mcp", f"{base}/mcp/", base, f"{base}/"}


# ---------- metadata (RFC 9728 + RFC 8414) ----------


def _protected_resource_metadata(request: Request) -> dict:
    base = public_base_url(request)
    return {
        "resource": canonical_resource(request),
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "resource_name": "Trixa",
        "resource_documentation": f"{base}/ui/settings",
    }


@router.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata(request: Request) -> dict:
    return _protected_resource_metadata(request)


@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata_for_mcp(request: Request) -> dict:
    """Samma dokument på den path-suffixade adressen.

    RFC 9728 lägger resursens path efter well-known-segmentet. Klienter provar
    olika varianter — servera båda hellre än att gissa.
    """
    return _protected_resource_metadata(request)


def _authorization_server_metadata(request: Request) -> dict:
    base = public_base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # Public clients: klienten autentiserar sig inte, PKCE gör jobbet.
        "token_endpoint_auth_methods_supported": ["none"],
        # Saknas den här raden vägrar klienter ansluta, oavsett vad som är rätt
        # i övrigt. Den är den vanligaste orsaken till "kan inte koppla upp".
        "code_challenge_methods_supported": ["S256"],
        "resource_indicators_supported": True,
        "service_documentation": f"{base}/ui/settings",
    }


@router.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata(request: Request) -> dict:
    return _authorization_server_metadata(request)


@router.get("/.well-known/oauth-authorization-server/mcp")
def authorization_server_metadata_for_mcp(request: Request) -> dict:
    return _authorization_server_metadata(request)


# ---------- Dynamisk klientregistrering (RFC 7591) ----------


def _valid_redirect_uri(uri: str) -> bool:
    """https överallt, http bara mot loopback (specens undantag för lokala appar)."""
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    if parsed.fragment or not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        return parsed.hostname in ("localhost", "127.0.0.1", "::1")
    # Egna scheman (mobilappar som återvänder till sig själva) tillåts, men
    # bara med ett riktigt värdnamn — inte "myapp:" utan mer.
    return bool(parsed.scheme and parsed.scheme not in ("http", "https"))


def _oauth_error(code: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description}, status_code=status
    )


@router.post("/oauth/register")
async def register_client(request: Request) -> Response:
    """Öppen DCR. Registrering ger ingen åtkomst — adepten måste ändå logga in
    och godkänna i /authorize innan klienten ser någon data."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _oauth_error("invalid_client_metadata", "Kunde inte tolka JSON-kroppen.")
    if not isinstance(body, dict):
        return _oauth_error("invalid_client_metadata", "Förväntade ett JSON-objekt.")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _oauth_error("invalid_redirect_uri", "redirect_uris krävs och måste vara en lista.")
    if len(redirect_uris) > MAX_REDIRECT_URIS:
        return _oauth_error("invalid_redirect_uri", f"Högst {MAX_REDIRECT_URIS} redirect_uris.")
    for uri in redirect_uris:
        if not isinstance(uri, str) or not _valid_redirect_uri(uri):
            return _oauth_error(
                "invalid_redirect_uri",
                f"Ogiltig redirect_uri: {uri!r}. Kräver https, eller http mot localhost.",
            )

    client = get_postgrest()
    try:
        recent = (
            client.table("oauth_clients")
            .select("client_id")
            .gte("created_at", _iso(_now() - timedelta(hours=1)))
            .limit(MAX_REGISTRATIONS_PER_HOUR + 1)
            .execute()
        )
        if len(recent.data or []) > MAX_REGISTRATIONS_PER_HOUR:
            return _oauth_error(
                "invalid_client_metadata",
                "För många registreringar just nu. Försök om en stund.",
                status=429,
            )
    except Exception:  # noqa: BLE001 — takkontrollen får inte blockera registrering
        logger.exception("Kunde inte räkna färska klientregistreringar")

    client_id = "trixa-client-" + secrets.token_urlsafe(16)
    row = {
        "client_id": client_id,
        "client_name": str(body.get("client_name") or "")[:120],
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "client_uri": str(body.get("client_uri") or "")[:500] or None,
    }
    try:
        client.table("oauth_clients").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Klientregistrering misslyckades")
        return _oauth_error("invalid_client_metadata", f"Kunde inte registrera: {exc}", 500)

    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(_now().timestamp()),
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "client_name": row["client_name"],
        },
        status_code=201,
    )


# ---------- /authorize ----------


def _load_client(client_id: str) -> dict | None:
    if not client_id:
        return None
    try:
        res = (
            get_postgrest()
            .table("oauth_clients")
            .select("client_id, client_name, redirect_uris")
            .eq("client_id", client_id)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("Klientuppslag misslyckades")
        return None
    return (res.data or [None])[0]


def _redirect_with_error(redirect_uri: str, state: str, code: str, description: str) -> RedirectResponse:
    params = {"error": code, "error_description": description}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=303)


def _fatal(request: Request, message: str, status: int = 400) -> HTMLResponse:
    """Fel som INTE får skickas vidare till en redirect_uri vi inte litar på.

    Okänd klient eller ej registrerad redirect_uri måste visas för användaren
    här — att omdirigera vore precis den öppna redirect vi vill undvika.
    """
    from trixa_api.ui import _render

    return _render(
        "oauth_error.html", {"request": request, "message": message}, status_code=status
    )


def _validate_authorize_params(request: Request, params: dict) -> tuple[dict | None, Any]:
    """Gemensam validering för GET (visa samtycke) och POST (verkställ beslut).

    Returnerar (validerad kontext, felrespons). Exakt en av dem är satt.
    Ordningen är medveten: klient och redirect_uri först, för allt därefter får
    rapporteras tillbaka till klienten — men bara till en adress vi känner igen.
    """
    client_id = (params.get("client_id") or "").strip()
    redirect_uri = (params.get("redirect_uri") or "").strip()

    client = _load_client(client_id)
    if client is None:
        return None, _fatal(
            request,
            "Okänd klient. Appen som skickade hit dig är inte registrerad hos Trixa.",
        )
    registered = client.get("redirect_uris") or []
    if not redirect_uri or redirect_uri not in registered:
        return None, _fatal(
            request,
            "Appens returadress matchar inte den som registrerades. "
            "Av säkerhetsskäl skickas du inte vidare dit.",
        )

    state = (params.get("state") or "").strip()
    if (params.get("response_type") or "").strip() != "code":
        return None, _redirect_with_error(
            redirect_uri, state, "unsupported_response_type", "Bara response_type=code stöds."
        )

    challenge = (params.get("code_challenge") or "").strip()
    method = (params.get("code_challenge_method") or "").strip() or "S256"
    if not challenge:
        return None, _redirect_with_error(
            redirect_uri, state, "invalid_request", "code_challenge krävs (PKCE)."
        )
    if method != "S256":
        return None, _redirect_with_error(
            redirect_uri, state, "invalid_request", "Bara code_challenge_method=S256 stöds."
        )

    resource = (params.get("resource") or "").strip()
    if resource and resource not in acceptable_resources(request):
        return None, _redirect_with_error(
            redirect_uri, state, "invalid_target",
            f"Okänd resurs: {resource}. Förväntade {canonical_resource(request)}.",
        )

    return {
        "client": client,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": method,
        # Alla accepterade stavningar (med/utan path, med/utan avslutande
        # snedstreck) är samma resurs. Lagrades råvärdet blev audience
        # "…/mcp/" medan /mcp bara godtog "…/mcp" — token utfärdad, token
        # avvisad, klienten fick börja om vid varje anrop. Kanonisera här,
        # så bär varje token samma audience oavsett hur klienten stavade.
        "resource": canonical_resource(request),
        "scope": (params.get("scope") or "").strip(),
    }, None


@router.get("/oauth/authorize")
def authorize_form(request: Request) -> Any:
    """Samtyckessidan. Auth-grinden i main.py har redan krävt inloggning."""
    ctx, error = _validate_authorize_params(request, dict(request.query_params))
    if error is not None:
        return error

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        # Ska inte kunna hända (grinden kör före), men hellre en tydlig retur
        # än ett samtycke utan känd adept.
        return _fatal(request, "Du måste vara inloggad för att godkänna åtkomst.", 401)

    from trixa_api.ui import _render

    athlete_name = None
    try:
        res = (
            get_postgrest().table("profiles").select("name").eq("id", user_id).limit(1).execute()
        )
        athlete_name = (res.data or [{}])[0].get("name")
    except Exception:  # noqa: BLE001 — namnet är kosmetik
        pass

    return _render(
        "oauth_consent.html",
        {
            "request": request,
            "client_name": ctx["client"].get("client_name") or "En AI-klient",
            "athlete_name": athlete_name,
            "params": dict(request.query_params),
        },
    )


@router.post("/oauth/authorize")
async def authorize_decision(request: Request) -> Any:
    """Adeptens beslut. Allt valideras om från formulärfälten — vi litar inte på
    att sidan som postar hit är den vi renderade."""
    form = dict(await request.form())
    ctx, error = _validate_authorize_params(request, form)
    if error is not None:
        return error

    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return _fatal(request, "Du måste vara inloggad för att godkänna åtkomst.", 401)

    if (form.get("decision") or "") != "approve":
        return _redirect_with_error(
            ctx["redirect_uri"], ctx["state"], "access_denied", "Adepten nekade åtkomst."
        )

    code = secrets.token_urlsafe(32)
    try:
        get_postgrest().table("oauth_authorization_codes").insert({
            "code_hash": _hash(code),
            "client_id": ctx["client_id"],
            "user_id": user_id,
            "redirect_uri": ctx["redirect_uri"],
            "code_challenge": ctx["code_challenge"],
            "code_challenge_method": ctx["code_challenge_method"],
            "resource": ctx["resource"],
            "scope": ctx["scope"],
            "expires_at": _iso(_now() + timedelta(seconds=CODE_TTL_SECONDS)),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kunde inte spara auktoriseringskod")
        return _redirect_with_error(
            ctx["redirect_uri"], ctx["state"], "server_error", f"Kunde inte utfärda kod: {exc}"
        )

    params = {"code": code}
    if ctx["state"]:
        params["state"] = ctx["state"]
    sep = "&" if "?" in ctx["redirect_uri"] else "?"
    return RedirectResponse(f"{ctx['redirect_uri']}{sep}{urlencode(params)}", status_code=303)


# ---------- /token ----------


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(expected, challenge)


def _revoke_family(client_id: str, user_id: str, reason: str) -> None:
    """Återkalla alla tokens för (klient, adept).

    Körs när en kod eller refresh-token återanvänds. Vi vet inte om det är
    angriparen eller den ärliga klienten som kom sist, så båda får börja om —
    det är OAuth 2.1:s rekommendation och rätt avvägning.
    """
    logger.warning("Återkallar token-familj för %s/%s: %s", client_id, user_id, reason)
    try:
        get_postgrest().table("oauth_tokens").update({"revoked_at": _iso(_now())}).eq(
            "client_id", client_id
        ).eq("user_id", user_id).is_("revoked_at", "null").execute()
    except Exception:  # noqa: BLE001
        logger.exception("Familjeåterkallning misslyckades")


def _issue_tokens(
    client_id: str, user_id: str, audience: str, scope: str,
    auth_code_hash: str | None = None, rotated_from: str | None = None,
) -> JSONResponse:
    access = "trixa_at_" + secrets.token_urlsafe(32)
    refresh = "trixa_rt_" + secrets.token_urlsafe(32)
    now = _now()
    row = {
        "access_token_hash": _hash(access),
        "refresh_token_hash": _hash(refresh),
        "client_id": client_id,
        "user_id": user_id,
        "audience": audience,
        "scope": scope or "",
        "auth_code_hash": auth_code_hash,
        "expires_at": _iso(now + timedelta(seconds=ACCESS_TTL_SECONDS)),
        "refresh_expires_at": _iso(now + timedelta(seconds=REFRESH_TTL_SECONDS)),
        "rotated_from": rotated_from,
    }
    get_postgrest().table("oauth_tokens").insert(row).execute()
    body = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
    }
    if scope:
        body["scope"] = scope
    # Tokens får aldrig cachas av mellanliggande lager.
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth/token")
async def token_endpoint(request: Request) -> Response:
    form = dict(await request.form())
    grant_type = (form.get("grant_type") or "").strip()
    if grant_type == "authorization_code":
        return _grant_authorization_code(request, form)
    if grant_type == "refresh_token":
        return _grant_refresh_token(request, form)
    return _oauth_error("unsupported_grant_type", f"Okänd grant_type: {grant_type!r}")


def _grant_authorization_code(request: Request, form: dict) -> Response:
    client_id = (form.get("client_id") or "").strip()
    code = (form.get("code") or "").strip()
    verifier = (form.get("code_verifier") or "").strip()
    redirect_uri = (form.get("redirect_uri") or "").strip()

    if not (client_id and code and verifier):
        return _oauth_error("invalid_request", "client_id, code och code_verifier krävs.")
    if _load_client(client_id) is None:
        return _oauth_error("invalid_client", "Okänd klient.", 401)

    db = get_postgrest()
    try:
        res = (
            db.table("oauth_authorization_codes")
            .select("*").eq("code_hash", _hash(code)).limit(1).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Koduppslag misslyckades")
        return _oauth_error("server_error", str(exc), 503)

    row = (res.data or [None])[0]
    if row is None:
        return _oauth_error("invalid_grant", "Okänd eller redan förbrukad kod.")
    if row.get("consumed_at"):
        # Återanvänd kod = koden kan ha läckt. Bränn allt som utfärdats på den.
        _revoke_family(row["client_id"], row["user_id"], "auktoriseringskod återanvänd")
        return _oauth_error("invalid_grant", "Koden är redan använd.")
    if row["client_id"] != client_id:
        return _oauth_error("invalid_grant", "Koden tillhör en annan klient.")
    if redirect_uri and redirect_uri != row["redirect_uri"]:
        return _oauth_error("invalid_grant", "redirect_uri matchar inte den i auktoriseringen.")
    if _parse_ts(row["expires_at"]) < _now():
        return _oauth_error("invalid_grant", "Koden har gått ut.")
    if not _verify_pkce(verifier, row["code_challenge"]):
        return _oauth_error("invalid_grant", "code_verifier matchar inte code_challenge.")

    # Förbrukningen måste vara atomisk: två samtidiga /token med samma kod
    # läste båda consumed_at=NULL ovan och skulle båda få tokens. Villkoret
    # på UPDATE:n gör att bara en vinner; den som får noll rader tillbaka
    # har mött en redan förbrukad kod och behandlas som återanvändning.
    try:
        consumed = db.table("oauth_authorization_codes").update(
            {"consumed_at": _iso(_now())}
        ).eq("code_hash", row["code_hash"]).is_("consumed_at", "null").execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kunde inte markera kod som förbrukad")
        return _oauth_error("server_error", str(exc), 503)
    if not consumed.data:
        _revoke_family(row["client_id"], row["user_id"], "auktoriseringskod återanvänd (kapplöpning)")
        return _oauth_error("invalid_grant", "Koden är redan använd.")

    return _issue_tokens(
        client_id=client_id,
        user_id=row["user_id"],
        audience=row.get("resource") or canonical_resource(request),
        scope=row.get("scope") or "",
        auth_code_hash=row["code_hash"],
    )


def _grant_refresh_token(request: Request, form: dict) -> Response:
    client_id = (form.get("client_id") or "").strip()
    refresh = (form.get("refresh_token") or "").strip()
    if not (client_id and refresh):
        return _oauth_error("invalid_request", "client_id och refresh_token krävs.")

    db = get_postgrest()
    try:
        res = (
            db.table("oauth_tokens")
            .select("*").eq("refresh_token_hash", _hash(refresh)).limit(1).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Refresh-uppslag misslyckades")
        return _oauth_error("server_error", str(exc), 503)

    row = (res.data or [None])[0]
    if row is None:
        return _oauth_error("invalid_grant", "Okänd refresh-token.")
    if row["client_id"] != client_id:
        return _oauth_error("invalid_grant", "Token tillhör en annan klient.")
    if row.get("revoked_at"):
        # En återkallad refresh används igen: antingen är den stulen eller så
        # har klienten tappat bort rotationen. Bränn familjen.
        _revoke_family(row["client_id"], row["user_id"], "återkallad refresh-token återanvänd")
        return _oauth_error("invalid_grant", "Token är återkallad.")
    if row.get("refresh_expires_at") and _parse_ts(row["refresh_expires_at"]) < _now():
        return _oauth_error("invalid_grant", "Refresh-token har gått ut.")

    # Samma atomicitet som för koden: rotationen får bara lyckas för den
    # som faktiskt återkallar raden. Förlorar vi kapplöpningen är token
    # redan roterad av någon annan — stulen eller en klient som tappat
    # rotationen — och familjen bränns, precis som vid en sen återanvändning.
    try:
        rotated = db.table("oauth_tokens").update({"revoked_at": _iso(_now())}).eq(
            "id", row["id"]
        ).is_("revoked_at", "null").execute()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kunde inte återkalla roterad token")
        return _oauth_error("server_error", str(exc), 503)
    if not rotated.data:
        _revoke_family(row["client_id"], row["user_id"], "refresh-token roterad samtidigt (kapplöpning)")
        return _oauth_error("invalid_grant", "Token är återkallad.")

    return _issue_tokens(
        client_id=client_id,
        user_id=row["user_id"],
        audience=row["audience"],
        scope=row.get("scope") or "",
        rotated_from=row["id"],
    )


def _parse_ts(value: str) -> datetime:
    """Postgres-timestamp → tz-medveten datetime. Naiva värden tolkas som UTC."""
    text = str(value).replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Hellre "utgången" än att krascha på ett format vi inte känner igen.
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------- resursserver-sidan: slå upp en access-token ----------


def resolve_oauth_token(raw: str, expected_audience: str) -> dict | None:
    """Giltig OAuth-access-token → raden. None om ogiltig, utgången, återkallad
    eller utfärdad för en annan resurs.

    Audience-kontrollen är inte formalia: utan den skulle en token som en adept
    gett ut till någon annan tjänst kunna spelas upp mot Trixa.
    """
    if not raw:
        return None
    try:
        res = (
            get_postgrest()
            .table("oauth_tokens")
            .select("id, user_id, client_id, audience, expires_at, revoked_at")
            .eq("access_token_hash", _hash(raw))
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("OAuth-tokenuppslag misslyckades")
        return None

    row = (res.data or [None])[0]
    if row is None or row.get("revoked_at"):
        return None
    if _parse_ts(row["expires_at"]) < _now():
        return None
    # Nya tokens bär alltid den kanoniska audiencen; äldre kan bära någon av
    # de stavningar authorize godtog. Alla pekar på samma server.
    base = expected_audience.rstrip("/")
    if base.endswith("/mcp"):
        base = base[: -len("/mcp")]
    accepted_audiences = {f"{base}/mcp", f"{base}/mcp/", base, f"{base}/"}
    if row.get("audience") not in accepted_audiences:
        logger.warning(
            "Avvisade token utfärdad för %s, förväntade %s",
            row.get("audience"), expected_audience,
        )
        return None

    try:
        get_postgrest().table("oauth_tokens").update(
            {"last_used_at": _iso(_now())}
        ).eq("id", row["id"]).execute()
    except Exception:  # noqa: BLE001 — best-effort, blockera aldrig anropet
        pass
    return row
