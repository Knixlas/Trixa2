"""Tester för MCP-servern (/mcp) — transport, verktygslista och scope.

Återanvänder den fejkade postgrest:en från test_agent_api (underscore-namn så
pytest inte samlar in dem två gånger). Verifierar att handskakningen fungerar,
att verktygen når samma logik som /agent/* (origin='nils', svensk sport-lagring)
och att en återkallad token stänger hela MCP-ytan — inte bara REST-ytan.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import _RECENT, UID, _C, _with_token  # noqa: E402


def _client_and_store():
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Niklas"}],
        "athlete_profiles": [
            {"id": "81b667bc", "user_id": UID, "garmin_athlete_id": "g1", "goal": "ironman"}
        ],
        "planned_sessions": [],
        "training_log": [
            # Inom 28-dagarsfönstret — faken filtrerar datum på riktigt nu (docs/12 I7).
            {"user_id": UID, "date": _RECENT, "sport": "Cykel",
             "duration_min": 60, "source": "tp"}
        ],
        "coach_athletes": [{"athlete_id": UID, "coach_id": "coach1", "status": "accepted"}],
    }
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.agent_auth as aa
    import trixa_api.agent_api as ag
    import trixa_api.mcp_server as mcp

    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    ag.get_postgrest = lambda: fake

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mcp.router)
    return TestClient(app, raise_server_exceptions=False), st, aa


def _rpc(client, headers, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def _call(client, headers, tool, args=None, req_id=1):
    return _rpc(client, headers, "tools/call",
                {"name": tool, "arguments": args or {}}, req_id)


def _payload(response):
    """tools/call-svarets text tillbaka till Python."""
    import json

    return json.loads(response.json()["result"]["content"][0]["text"])


def test_auth_required():
    c, st, aa = _client_and_store()
    r = _rpc(c, {}, "tools/list")
    assert r.status_code == 401, r.status_code
    assert r.headers.get("WWW-Authenticate", "").startswith("Bearer")
    assert _rpc(c, {"Authorization": "Bearer trixa_nope"}, "tools/list").status_code == 401


def test_initialize_handshake():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = _rpc(c, H, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18", res["protocolVersion"]
    assert res["serverInfo"]["name"] == "trixa"
    assert "tools" in res["capabilities"]


def test_initialize_falls_back_on_unknown_protocol():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    res = _rpc(c, H, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert res["protocolVersion"] == "2025-06-18", res["protocolVersion"]


def test_notification_gets_no_body():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/mcp", headers=H,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202, r.status_code
    assert not r.content


def test_tools_list_shape():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    tools = _rpc(c, H, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"whoami", "get_athlete", "get_constraints", "get_week",
            "get_training_log", "get_recovery", "plan_session",
            "delete_planned_session", "log_override"} == names, names
    for t in tools:
        assert t["description"].strip(), t["name"]
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_read_tools_are_scoped_to_token():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    assert _payload(_call(c, H, "whoami"))["user_id"] == UID
    assert _payload(_call(c, H, "get_athlete"))["goal"] == "ironman"
    assert len(_payload(_call(c, H, "get_training_log"))["sessions"]) == 1
    assert "sessions" in _payload(_call(c, H, "get_week"))


def test_plan_session_writes_like_agent_api():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    out = _payload(_call(c, H, "plan_session", {
        "date": "2026-06-20", "sport": "bike", "title": "Z2", "duration_min": 90}))
    assert out["status"] == "ok", out
    row = st["planned_sessions"][0]
    assert row["sport"] == "Cykel", row["sport"]   # engelska in → svensk lagring
    assert row["origin"] == "nils", row["origin"]
    # upsert: samma dag + gren skrivs över, inte dubbleras
    _call(c, H, "plan_session", {"date": "2026-06-20", "sport": "bike", "title": "Ändrat"})
    assert len(st["planned_sessions"]) == 1
    assert st["planned_sessions"][0]["title"] == "Ändrat"


def test_override_uses_athlete_profiles_id():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    out = _payload(_call(c, H, "log_override", {
        "scope": "volume", "engine_recommendation": {"h": 12},
        "override_decision": {"h": 10}, "motivation": "återhämtning krävs nu"}))
    assert out["status"] == "ok", out
    assert st["coach_overrides"][0]["athlete_id"] == "81b667bc"  # ej user_id


def test_tool_errors_come_back_as_tool_errors():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    # Okänt verktyg → isError, inte JSON-RPC-fel (modellen ska kunna välja om)
    r = _call(c, H, "finns_inte")
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True

    # Trasigt datum → isError med begripligt meddelande
    bad = _call(c, H, "plan_session", {"date": "20 juni", "sport": "run", "title": "Löp"})
    assert bad.json()["result"]["isError"] is True
    assert "YYYY-MM-DD" in bad.json()["result"]["content"][0]["text"]

    # Saknat pass → HTTPException blir isError, inte 500
    gone = _call(c, H, "delete_planned_session", {"session_id": "nope"})
    assert gone.json()["result"]["isError"] is True


def test_unknown_method_is_jsonrpc_error():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = _rpc(c, H, "does/not/exist")
    assert r.json()["error"]["code"] == -32601, r.json()


def test_get_and_delete_are_405():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    assert c.get("/mcp", headers=H).status_code == 405
    assert c.delete("/mcp", headers=H).status_code == 405


def test_revoke_closes_mcp_surface():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    assert _rpc(c, H, "tools/list").status_code == 200
    st["api_tokens"][0]["revoked_at"] = "2026-06-15T00:00:00Z"
    assert _rpc(c, H, "tools/list").status_code == 401
    assert _call(c, H, "get_athlete").status_code == 401


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
