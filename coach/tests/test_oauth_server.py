"""Tester för OAuth 2.1-auktoriseringsservern framför /mcp.

Täcker hela kedjan en AI-klient går: 401 med resource_metadata → metadata →
DCR → authorize → token → anrop mot /mcp. Plus angreppen servern ska stoppa:
okänd klient, oregistrerad redirect_uri, PKCE-fel, återanvänd kod, roterad
refresh, och token utfärdad för fel resurs.

Fejkad postgrest från test_agent_api (underscore-namn → ingen dubbelinsamling).
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ["TRIXA_PUBLIC_URL"] = "https://trixa.test"

from coach.tests.test_agent_api import UID, _C  # noqa: E402

RESOURCE = "https://trixa.test/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "abcdefghijklmnopqrstuvwxyz012345678901234567890123"


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _client_and_store(logged_in: bool = True):
    st = {
        "oauth_clients": [],
        "oauth_authorization_codes": [],
        "oauth_tokens": [],
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Niklas"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID, "goal": "ironman"}],
        "planned_sessions": [],
        "training_log": [],
    }
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.agent_api as ag
    import trixa_api.agent_auth as aa
    import trixa_api.mcp_server as mcp
    import trixa_api.oauth_server as oauth

    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    ag.get_postgrest = lambda: fake
    oauth.get_postgrest = lambda: fake

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def fake_session(request: Request, call_next):
        # Speglar auth-grinden i main.py: /oauth/authorize kräver inloggning.
        request.state.user_id = UID if logged_in else None
        return await call_next(request)

    app.include_router(oauth.router)
    app.include_router(mcp.router)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False), st


def _register(c, redirect_uris=None) -> str:
    r = c.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": redirect_uris or [REDIRECT]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize_params(client_id, **overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
        "state": "xyz",
        "resource": RESOURCE,
    }
    params.update(overrides)
    return params


def _approve(c, client_id, **overrides) -> str:
    """Kör igenom samtycket och plocka ut koden ur redirecten."""
    params = _authorize_params(client_id, **overrides)
    r = c.post("/oauth/authorize", data={**params, "decision": "approve"})
    assert r.status_code == 303, r.text
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert "code" in query, query
    return query["code"][0]


def _exchange(c, client_id, code, verifier=VERIFIER):
    return c.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id,
        "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT})


# ---------- discovery ----------


def test_401_points_at_resource_metadata():
    c, st = _client_and_store()
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    header = r.headers.get("WWW-Authenticate", "")
    # Utan den här pekaren kan en OAuth-klient inte hitta auth-servern alls.
    assert 'resource_metadata="https://trixa.test/.well-known/oauth-protected-resource"' in header, header


def test_protected_resource_metadata():
    c, st = _client_and_store()
    for path in ("/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-protected-resource/mcp"):
        body = c.get(path).json()
        assert body["resource"] == RESOURCE, body
        assert body["authorization_servers"] == ["https://trixa.test"], body


def test_authorization_server_metadata_has_s256():
    c, st = _client_and_store()
    body = c.get("/.well-known/oauth-authorization-server").json()
    # Saknas S256 vägrar klienter ansluta även om allt annat är rätt.
    assert body["code_challenge_methods_supported"] == ["S256"], body
    assert "none" in body["token_endpoint_auth_methods_supported"], body
    assert body["registration_endpoint"] == "https://trixa.test/oauth/register"
    assert set(body["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert body["issuer"] == "https://trixa.test"


# ---------- registrering ----------


def test_dynamic_registration_returns_public_client():
    c, st = _client_and_store()
    r = c.post("/oauth/register", json={
        "client_name": "Claude", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["token_endpoint_auth_method"] == "none", "public client, ingen secret"
    assert "client_secret" not in body
    assert st["oauth_clients"][0]["redirect_uris"] == [REDIRECT]


def test_registration_rejects_unsafe_redirect_uris():
    c, st = _client_and_store()
    for bad in ("http://evil.example/cb", "https://evil.example/cb#frag", "", "notaurl"):
        r = c.post("/oauth/register", json={"redirect_uris": [bad]})
        assert r.status_code == 400, f"{bad!r} borde avvisas: {r.text}"
    # http mot loopback är specens undantag för lokala appar
    assert c.post("/oauth/register",
                  json={"redirect_uris": ["http://localhost:8765/cb"]}).status_code == 201


def test_registration_requires_redirect_uris():
    c, st = _client_and_store()
    assert c.post("/oauth/register", json={"client_name": "x"}).status_code == 400


# ---------- authorize ----------


def test_consent_page_shown_when_logged_in():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.get("/oauth/authorize", params=_authorize_params(cid))
    assert r.status_code == 200, r.text
    assert "Ge åtkomst" in r.text
    assert "Claude" in r.text


def test_unknown_client_is_not_redirected():
    c, st = _client_and_store()
    r = c.get("/oauth/authorize", params=_authorize_params("finns-inte"))
    # Måste visas för användaren — att omdirigera vore en öppen redirect.
    assert r.status_code == 400, r.status_code
    assert "location" not in r.headers


def test_unregistered_redirect_uri_is_not_redirected():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.get("/oauth/authorize",
              params=_authorize_params(cid, redirect_uri="https://evil.example/steal"))
    assert r.status_code == 400, r.status_code
    assert "location" not in r.headers


def test_missing_pkce_is_rejected():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.get("/oauth/authorize", params=_authorize_params(cid, code_challenge=""))
    assert r.status_code == 303
    assert "error=invalid_request" in r.headers["location"]


def test_plain_pkce_is_rejected():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.get("/oauth/authorize",
              params=_authorize_params(cid, code_challenge_method="plain"))
    assert r.status_code == 303
    assert "error=invalid_request" in r.headers["location"]


def test_wrong_resource_is_rejected():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.get("/oauth/authorize",
              params=_authorize_params(cid, resource="https://someone-else.example/mcp"))
    assert r.status_code == 303
    assert "error=invalid_target" in r.headers["location"]


def test_deny_sends_access_denied():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.post("/oauth/authorize", data={**_authorize_params(cid), "decision": "deny"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "error=access_denied" in loc and "state=xyz" in loc
    assert st["oauth_authorization_codes"] == [], "ingen kod får utfärdas vid nekande"


def test_approve_returns_code_and_state():
    c, st = _client_and_store()
    cid = _register(c)
    r = c.post("/oauth/authorize", data={**_authorize_params(cid), "decision": "approve"})
    assert r.status_code == 303
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["state"] == ["xyz"]
    assert len(st["oauth_authorization_codes"]) == 1
    # Bara hashen lagras — råkoden får aldrig ligga i databasen.
    assert st["oauth_authorization_codes"][0]["code_hash"] != query["code"][0]


# ---------- token ----------


def test_full_flow_yields_a_working_mcp_token():
    c, st = _client_and_store()
    cid = _register(c)
    code = _approve(c, cid)
    r = _exchange(c, cid, code)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert r.headers.get("Cache-Control") == "no-store"

    H = {"Authorization": f"Bearer {body['access_token']}"}
    tools = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert tools.status_code == 200, tools.text
    who = c.post("/mcp", headers=H, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}}})
    assert UID in who.json()["result"]["content"][0]["text"]


def test_wrong_verifier_is_rejected():
    c, st = _client_and_store()
    cid = _register(c)
    code = _approve(c, cid)
    r = _exchange(c, cid, code, verifier="fel-verifierare-som-inte-matchar-alls-1234")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_code_is_single_use_and_reuse_burns_the_family():
    c, st = _client_and_store()
    cid = _register(c)
    code = _approve(c, cid)
    first = _exchange(c, cid, code)
    assert first.status_code == 200
    second = _exchange(c, cid, code)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    # Återanvänd kod = möjlig läcka: allt som utfärdats ska vara dött.
    H = {"Authorization": f"Bearer {first.json()['access_token']}"}
    assert c.post("/mcp", headers=H,
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_code_from_another_client_is_rejected():
    c, st = _client_and_store()
    cid_a = _register(c)
    cid_b = _register(c, ["https://other.example/cb"])
    code = _approve(c, cid_a)
    r = _exchange(c, cid_b, code)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_refresh_rotates_and_old_token_dies():
    c, st = _client_and_store()
    cid = _register(c)
    first = _exchange(c, cid, _approve(c, cid)).json()

    second = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "refresh_token": first["refresh_token"]})
    assert second.status_code == 200, second.text
    new = second.json()
    assert new["refresh_token"] != first["refresh_token"], "refresh måste roteras"

    # Den nya fungerar, den gamla access-token är död.
    assert c.post("/mcp", headers={"Authorization": f"Bearer {new['access_token']}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200
    assert c.post("/mcp", headers={"Authorization": f"Bearer {first['access_token']}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_reused_refresh_token_burns_the_family():
    c, st = _client_and_store()
    cid = _register(c)
    first = _exchange(c, cid, _approve(c, cid)).json()
    second = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "refresh_token": first["refresh_token"]}).json()

    # Samma refresh igen: antingen stulen eller trasig klient — bränn allt.
    replay = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "refresh_token": first["refresh_token"]})
    assert replay.status_code == 400
    assert c.post("/mcp", headers={"Authorization": f"Bearer {second['access_token']}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_unsupported_grant_type():
    c, st = _client_and_store()
    r = c.post("/oauth/token", data={"grant_type": "password"})
    assert r.json()["error"] == "unsupported_grant_type"


def test_token_for_another_audience_is_rejected():
    """Kärnan i RFC 8707: en token utfärdad för någon annan resurs får inte
    fungera mot oss, även om den är giltig i övrigt."""
    c, st = _client_and_store()
    cid = _register(c)
    access = _exchange(c, cid, _approve(c, cid)).json()["access_token"]
    H = {"Authorization": f"Bearer {access}"}
    assert c.post("/mcp", headers=H,
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200

    st["oauth_tokens"][0]["audience"] = "https://someone-else.example/mcp"
    assert c.post("/mcp", headers=H,
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_personal_tokens_still_work_alongside_oauth():
    """Claude Code-vägen får inte brytas av att OAuth tillkommit."""
    c, st = _client_and_store()
    import trixa_api.agent_auth as aa

    raw, token_hash, prefix = aa.generate_token()
    st["api_tokens"].append({"id": "tok1", "user_id": UID, "name": "Claude Code",
                             "token_hash": token_hash, "token_prefix": prefix,
                             "revoked_at": None})
    r = c.post("/mcp", headers={"Authorization": f"Bearer {raw}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200, r.text


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
