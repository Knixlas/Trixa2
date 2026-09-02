"""Säkerhetsfynden ur kodöversynen 2026-09-02 (docs/12, avsnitt A).

A1  /health/integrations läckte alla adepters user_id + TP-felsträngar utan
    inloggning. Nu: aggregat utan token, detaljer bara med TRIXA_OPS_TOKEN.
A2  Audience-kollen avvisade resource-stavningar som authorize godtog →
    evig omloggning. Nu kanoniseras audience vid utfärdande, och äldre
    tokens med alias-audience accepteras.
A3  Kod-konsumtion och refresh-rotation var check-then-write. Nu villkorade
    UPDATE:ar; den som förlorar kapplöpningen bränner familjen.
A4  planned_session_id togs emot utan ägarkoll.
A6  Bekräftelsetexten i health.html gick genom HTML-escape in i ett
    JS-strängliteral (self-XSS).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ["TRIXA_PUBLIC_URL"] = "https://trixa.test"

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402
from coach.tests.test_oauth_server import (  # noqa: E402
    _approve, _client_and_store, _exchange, _register,
)

TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


# ---------- A1: /health/integrations ----------


def _main_client(store: dict):
    import coach.trixa.db as db
    import trixa_api.main as main

    fake = _C(store)
    db.get_postgrest = lambda: fake
    main.get_postgrest = lambda: fake
    from fastapi.testclient import TestClient

    return TestClient(main.app, raise_server_exceptions=False)


_RUNS = [{
    "user_id": UID, "integration": "trainingpeaks", "operation": "read_sync",
    "status": "success", "started_at": "2099-01-01T00:00:00Z",
    "finished_at": "2099-01-01T00:05:00Z", "records_processed": 3,
    "error_message": "cookie expired: <secret>", "metadata": {"tp": "x"},
}]


def test_health_integrations_utan_token_ger_bara_aggregat(monkeypatch):
    monkeypatch.delenv("TRIXA_OPS_TOKEN", raising=False)
    c = _main_client({"integration_runs": list(_RUNS)})
    body = c.get("/health/integrations").json()
    assert body["status"] == "ok"
    assert body["users_ok"] == 1
    assert "users" not in body
    assert UID not in c.get("/health/integrations").text
    assert "cookie expired" not in c.get("/health/integrations").text


def test_health_integrations_med_token_ger_detaljer(monkeypatch):
    monkeypatch.setenv("TRIXA_OPS_TOKEN", "hemlig")
    c = _main_client({"integration_runs": list(_RUNS)})
    assert "users" not in c.get("/health/integrations").json()
    assert "users" not in c.get(
        "/health/integrations", headers={"X-Trixa-Ops-Token": "fel"}
    ).json()
    body = c.get("/health/integrations", headers={"X-Trixa-Ops-Token": "hemlig"}).json()
    assert body["users"][0]["user_id"] == UID


# ---------- A2: audience ----------


def test_resource_med_snedstreck_ger_token_som_mcp_godtar():
    """RFC 8707 tillåter '…/mcp/'; authorize godtog det, /mcp avvisade
    tokenen → klienten började om vid varje anrop."""
    c, st = _client_and_store()
    cid = _register(c)
    code = _approve(c, cid, resource="https://trixa.test/mcp/")
    access = _exchange(c, cid, code).json()["access_token"]
    assert st["oauth_tokens"][0]["audience"] == "https://trixa.test/mcp"  # kanonisk
    r = c.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json=TOOLS_LIST)
    assert r.status_code == 200, r.text


def test_aldre_token_med_alias_audience_accepteras_fortfarande():
    c, st = _client_and_store()
    cid = _register(c)
    access = _exchange(c, cid, _approve(c, cid)).json()["access_token"]
    st["oauth_tokens"][0]["audience"] = "https://trixa.test/"
    r = c.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json=TOOLS_LIST)
    assert r.status_code == 200


def test_frammande_audience_avvisas_fortfarande():
    c, st = _client_and_store()
    cid = _register(c)
    access = _exchange(c, cid, _approve(c, cid)).json()["access_token"]
    st["oauth_tokens"][0]["audience"] = "https://someone-else.example/mcp"
    assert c.post("/mcp", headers={"Authorization": f"Bearer {access}"},
                  json=TOOLS_LIST).status_code == 401


# ---------- A3: atomisk konsumtion/rotation ----------


def test_kod_som_redan_forbrukats_i_databasen_avvisas_och_branner_familjen():
    """Simulerar förloraren i kapplöpningen: raden är NULL vid läsningen men
    förbrukad när UPDATE:n körs. Fejken körs sekventiellt, så vi sätter
    consumed_at mellan select och update genom att förbruka koden en gång
    till med ett redan utfärdat token-par i familjen."""
    c, st = _client_and_store()
    cid = _register(c)
    code = _approve(c, cid)
    first = _exchange(c, cid, code)
    assert first.status_code == 200
    assert st["oauth_tokens"][0].get("revoked_at") is None
    second = _exchange(c, cid, code)          # samma kod igen
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    assert all(t.get("revoked_at") for t in st["oauth_tokens"])   # familjen bränd


def test_refresh_rotation_ar_villkorad_pa_orevokerad_rad():
    c, st = _client_and_store()
    cid = _register(c)
    first = _exchange(c, cid, _approve(c, cid)).json()
    # Någon annan hann rotera: raden är redan återkallad när vår UPDATE går.
    st["oauth_tokens"][0]["revoked_at"] = "2026-09-02T00:00:00+00:00"
    r = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "client_id": cid,
        "refresh_token": first["refresh_token"]})
    assert r.status_code == 400
    assert len(st["oauth_tokens"]) == 1          # inget nytt par utfärdat


# ---------- A4: ägarkoll på planned_session_id ----------


def _agent_client(store: dict):
    st = {
        "api_tokens": [], "profiles": [{"id": UID, "name": "Adept"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
        "coach_athletes": [], "coach_overrides": [], **store,
    }
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.agent_auth as aa
    import trixa_api.agent_api as ag

    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    ag.get_postgrest = lambda: fake
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ag.router)
    return TestClient(app, raise_server_exceptions=False), st, aa


def _override(planned_id: str) -> dict:
    return {
        "scope": "workout", "planned_session_id": planned_id,
        "engine_recommendation": {"category": "AE"},
        "override_decision": {"category": "rest"},
        "motivation": "Deltoidsmärta — passet byts mot vila.",
        "medical_context_disclosed": True, "athlete_explicit_request": False,
    }


def test_override_pa_annan_adepts_pass_avvisas():
    c, st, aa = _agent_client({"planned_sessions": [
        {"id": "ps-b", "user_id": "someone-else", "date": "2026-09-03", "sport": "Styrka"},
    ]})
    r = c.post("/agent/override", headers=_with_token(st, aa), json=_override("ps-b"))
    assert r.status_code == 404, r.text
    assert st["coach_overrides"] == []


def test_override_pa_eget_pass_gar_igenom():
    c, st, aa = _agent_client({"planned_sessions": [
        {"id": "ps-a", "user_id": UID, "date": "2026-09-03", "sport": "Styrka"},
    ]})
    r = c.post("/agent/override", headers=_with_token(st, aa), json=_override("ps-a"))
    assert r.status_code == 200, r.text
    assert st["coach_overrides"][0]["planned_session_id"] == "ps-a"


# ---------- A6: bekräftelsetext i JS ----------


def test_health_confirm_text_ar_js_escapad():
    from coach.tests.test_health_edit import _client_and_store as _health_client

    evil = "');alert(1)//"
    c, _ = _health_client(concerns=[{
        "id": "c1", "name": evil, "severity": 2, "since_date": "2026-08-01",
        "locations": ["knee"], "notes": "", "needs_followup": False,
    }])
    html = c.get("/ui/health").text
    assert "confirm('Ta bort concern " not in html      # gamla, osäkra formen
    assert f"confirm('Ta bort concern {evil}" not in html
    assert "\\u0027" in html                             # tojson-escapad apostrof
