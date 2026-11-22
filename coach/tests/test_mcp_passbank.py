"""MCP-coachen planerar inom träningsläran: motorns vecka, passbanken, nivån.

Förut kunde en AI-coach via MCP bara skriva fri text — plan_session tog
emot vad som helst och workout_code kontrollerades inte. Coachens pass
vinner över motorn, så en coach kunde ge en nybörjare fem långa pass på
fackspråk, precis det docs/13 rättade i motorn.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _client_and_store, _with_token  # noqa: E402

MONDAY = date(2026, 11, 23)


def _beginner(st, level="beginner"):
    st["athlete_profiles"][0].update({
        "experience_level": level, "goal": "first_race", "weekly_hours": 6,
        "sports": ["run"], "recovery_week_ratio": "3:1", "preferred_rest_days": [],
        "equipment": {}, "preferred_settings": {}, "active_concerns": [],
        "phase_state": {}, "onboarded_at": "2026-11-22T00:00:00Z",
    })
    race_date = (MONDAY + timedelta(weeks=38, days=5)).isoformat()
    st["athlete_profiles"][0]["race_date"] = race_date
    st["races"] = [{"athlete_id": "81b667bc", "date": race_date, "priority": "A",
                    "name": "Milen", "distance": "10k"}]


def _client(level="beginner"):
    c, st, aa = _client_and_store()
    _beginner(st, level)
    return c, st, _with_token(st, aa)


# ---------- passbanken ----------


def test_passbanken_filtreras_pa_niva():
    c, st, H = _client()
    r = c.get("/agent/passbank", headers=H, params={"sport": "run"})
    assert r.status_code == 200, r.text
    body = r.json()
    by_code = {w["workout_code"]: w for w in body["workouts"]}
    assert body["level"] == "beginner"
    assert "AE1_run_gajogg" in by_code
    assert by_code["AE1_run_gajogg"]["allowed_for_level"] is True
    assert any(w["category"] == "ME" and not w["allowed_for_level"] for w in body["workouts"])


def test_nyborjarpass_syns_inte_for_avancerade():
    c, st, H = _client("advanced")
    codes = {w["workout_code"] for w in c.get("/agent/passbank", headers=H).json()["workouts"]}
    assert "AE1_run_gajogg" not in codes
    assert "AE2_run_template" in codes


# ---------- plan_session med passkod ----------


def test_pass_med_kod_far_text_steg_och_langd_ur_passbanken():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "run", "title": "",
        "workout_code": "AE1_run_gajogg", "duration_min": 25,
        "details": "Ta det lugnt, det är första veckan.",
    })
    assert r.status_code == 200, r.text
    row = next(s for s in st["planned_sessions"] if s["date"] == "2026-11-24")
    assert row["title"] == "Gå och jogga"
    assert row["duration_min"] == 25
    assert "1 min lugn jogg, 1,5 min gång" in row["details"]
    assert "**Från din coach:**" in row["details"]
    assert row["steps"]
    assert row["origin"] == "nils"
    assert not r.json()["warnings"], r.json()["warnings"]


def test_langd_utanfor_passets_spann_flyttas_in_med_varning():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "run", "title": "",
        "workout_code": "AE1_run_gajogg", "duration_min": 5,
    })
    assert r.status_code == 200, r.text
    assert st["planned_sessions"][0]["duration_min"] == 15
    assert any("spann" in w for w in r.json()["warnings"])


def test_okand_passkod_avvisas():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "run", "title": "X", "workout_code": "PÅHITTAT_01",
    })
    assert r.status_code == 400
    assert "list_workouts" in r.text
    assert st["planned_sessions"] == []


def test_passkod_i_fel_gren_avvisas():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "bike", "title": "X", "workout_code": "AE1_run_gajogg",
    })
    assert r.status_code == 400


# ---------- nivåvarningar ----------


def test_nyborjare_med_langt_fritt_pass_far_varningar():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "run", "title": "Långpass", "duration_min": 100,
    })
    assert r.status_code == 200, r.text
    w = " | ".join(r.json()["warnings"])
    assert "inte ur passbanken" in w
    assert "längsta passet" in w
    assert "budget" in w
    assert "log_override" in w


def test_nyborjare_med_troskelpass_far_varning_om_passtyp():
    c, st, H = _client()
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-11-24", "sport": "run", "title": "", "workout_code": "ME1_run_01",
    })
    assert r.status_code == 200, r.text
    assert any("Tröskel" in w and "ingår inte" in w for w in r.json()["warnings"])


def test_fjarde_konditionspasset_for_nyborjare_varnar():
    c, st, H = _client()
    for i in range(4):
        r = c.post("/agent/plan/session", headers=H, json={
            "date": (MONDAY + timedelta(days=i)).isoformat(), "sport": "run", "title": "",
            "workout_code": "AE1_run_gajogg", "duration_min": 15,
        })
    assert any("högst 3" in w for w in r.json()["warnings"])


# ---------- motorns vecka ----------


def test_motorns_vecka_visar_fas_nivagranser_och_pass(monkeypatch):
    c, st, H = _client()
    from coach.trixa import clock, planner
    from coach.tests.test_agent_api import _C

    monkeypatch.setattr(planner, "get_supabase", lambda: _C(st))
    monkeypatch.setattr(clock, "today", lambda: MONDAY - timedelta(days=1))
    r = c.get("/agent/engine/week", headers=H, params={"monday": MONDAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] is False
    assert body["level_limits"]["level"] == "beginner"
    assert body["level_limits"]["max_session_min"]["run"] == 75
    assert body["level_limits"]["budget_minutes"] == 75
    runs = [s for s in body["sessions"] if s["sport"] == "run"]
    assert runs and all(s["workout_code"] == "AE1_run_gajogg" for s in runs)
    assert st["planned_sessions"] == []   # torrkört: inget sparat


def test_mcp_verktyget_list_workouts_svarar():
    import trixa_api.mcp_server as mcp
    from trixa_api.agent_auth import AgentScope

    _client()   # kopplar faken
    out = mcp._call_tool(
        AgentScope(user_id=UID, token_id="tok1", name="Nils"),
        {"name": "list_workouts", "arguments": {"sport": "run"}},
    )
    assert not out.get("isError"), out
    assert "AE1_run_gajogg" in out["content"][0]["text"]
