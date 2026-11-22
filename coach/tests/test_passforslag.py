"""Passkänsla och AI-coachers förslag till passbanken (migration 014).

Coacherna får föreslå nya pass, helst sådana adepten gillat — men inget går
in i passbanken utan granskning, underlaget räknas av servern, och förslaget
prövas mot samma form- och språkregler som passbanken.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _client_and_store, _with_token  # noqa: E402


def _seed(st, again=("yes", "yes", "maybe")):
    st["profiles"][0]["name"] = "Kristina Exempelsson"
    st["athlete_profiles"][0].update({
        "experience_level": "beginner", "active_concerns": [{"name": "Hälseneinflammation"}],
        "health_conditions": [], "phase_state": {},
    })
    st["planned_sessions"] = [
        {"id": f"ps{i}", "user_id": UID, "date": f"2026-11-{10 + i}", "sport": "Löpning",
         "title": "Gå och jogga", "workout_code": "AE1_run_gajogg", "status": "completed",
         "origin": "trixa2", "duration_min": 25}
        for i in range(3)
    ]
    st["training_log"] = [
        {"user_id": UID, "date": f"2026-11-{10 + i}", "sport": "Löpning", "duration_min": 25,
         "source": "manual", "planned_session_id": f"ps{i}", "rpe": 4 + i}
        for i in range(3)
    ]
    st["session_feedback"] = [
        {"user_id": UID, "planned_session_id": f"ps{i}", "workout_code": "AE1_run_gajogg",
         "session_date": f"2026-11-{10 + i}", "again": a}
        for i, a in enumerate(again)
    ]
    st["workout_proposals"] = []


def _proposal(**over):
    body = {
        "sport": "run", "category": "AE",
        "name": "Lugn runda med tre korta backar",
        "intent": "En lugn runda där du tar tre korta backar i lugn takt. Gå uppför om det behövs.",
        "main_set": [
            {"segment": "warmup", "duration_pct": 0.2, "zone": 1, "description": "Gå raskt och jogga sedan lugnt."},
            {"segment": "continuous", "duration_pct": 0.65, "zone": 2,
             "description": "Spring lugnt. Ta tre backar på vägen, gå uppför om det behövs."},
            {"segment": "cooldown", "duration_pct": 0.15, "zone": 1, "description": "Gå hem lugnt."},
        ],
        "duration_min": {"default": 35, "range": [25, 50]},
        "abort_conditions": ["Ont i knä eller fot som gör att du haltar → gå hem"],
        "rationale": "Adepten tycker om backarna på gå/jogg-passen och vill ha mer variation.",
        "source_session_ids": ["ps0", "ps1"],
    }
    body.update(over)
    return body


def _client():
    c, st, aa = _client_and_store()
    _seed(st)
    return c, st, _with_token(st, aa)


# ---------- passkänslan ----------


def test_svaret_sparas_och_byts_ut():
    from trixa_api.ui import save_session_feedback

    st = {"session_feedback": []}
    fake = _C(st)
    planned = {"id": "ps1", "date": "2026-11-24", "workout_code": "AE1_run_gajogg"}
    save_session_feedback(fake, UID, planned, "yes")
    save_session_feedback(fake, UID, planned, "no")
    save_session_feedback(fake, UID, planned, "kanske-inte")   # ogiltigt ignoreras
    assert len(st["session_feedback"]) == 1
    assert st["session_feedback"][0]["again"] == "no"
    assert st["session_feedback"][0]["workout_code"] == "AE1_run_gajogg"


def test_coachen_ser_svaren_i_veckan_och_i_sammanstallningen():
    c, st, H = _client()
    week = c.get("/agent/week", headers=H, params={"monday": "2026-11-09"}).json()
    by_id = {s["id"]: s for s in week["sessions"]}
    assert by_id["ps0"]["feedback_again"] == "yes"
    summary = c.get("/agent/feedback", headers=H, params={"days": 365}).json()
    assert summary["by_kind"][0] == {"kind": "AE1_run_gajogg", "yes": 2, "maybe": 1, "no": 0}


# ---------- förslaget ----------


def test_giltigt_forslag_sparas_som_vantande_med_serverns_underlag():
    c, st, H = _client()
    r = c.post("/agent/proposals", headers=H, json=_proposal())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["evidence"]["done"] == 2
    assert body["evidence"]["again"] == {"yes": 2, "maybe": 0, "no": 0}
    assert body["evidence"]["avg_rpe"] == 4.5
    assert "Lugn runda med tre korta backar" in body["preview"]
    row = st["workout_proposals"][0]
    assert row["status"] == "pending" and row["workout"]["code"].startswith("FORSLAG_run_")


def test_fackord_avvisas_med_lista():
    c, st, H = _client()
    r = c.post("/agent/proposals", headers=H, json=_proposal(
        intent="Aerob Z2 med strides, HR under tröskeln."))
    assert r.status_code == 400
    assert "fackord" in r.text
    assert st["workout_proposals"] == []


def test_adeptens_namn_eller_halsa_avvisas():
    c, st, H = _client()
    r = c.post("/agent/proposals", headers=H, json=_proposal(
        rationale="ok", intent="Passet Kristina gillar mest. Lugnt hela vägen."))
    assert r.status_code == 400
    assert "namn eller hälsouppgifter" in r.text
    r = c.post("/agent/proposals", headers=H, json=_proposal(
        abort_conditions=["Känns hälseneinflammation av → avbryt"]))
    assert r.status_code == 400


def test_forslag_maste_bygga_pa_gjorda_egna_pass():
    c, st, H = _client()
    assert c.post("/agent/proposals", headers=H, json=_proposal(source_session_ids=[])).status_code == 400
    assert c.post("/agent/proposals", headers=H,
                  json=_proposal(source_session_ids=["nagon-annans"])).status_code == 400
    st["training_log"] = []
    assert c.post("/agent/proposals", headers=H, json=_proposal()).status_code == 400


def test_forslag_utan_ja_far_varning():
    c, st, H = _client()
    st["session_feedback"] = [dict(r, again="maybe") for r in st["session_feedback"]]
    r = c.post("/agent/proposals", headers=H, json=_proposal())
    assert r.status_code == 200
    assert any("\"ja\"" in w for w in r.json()["warnings"])


def test_coachen_ser_sina_forslag():
    c, st, H = _client()
    c.post("/agent/proposals", headers=H, json=_proposal())
    rows = c.get("/agent/proposals", headers=H).json()["proposals"]
    assert rows and rows[0]["status"] == "pending"


# ---------- granskning ----------


def test_granskning_godkanner_en_gang():
    from fastapi import HTTPException

    from trixa_api.agent_proposals import review

    st = {"workout_proposals": [{"id": "p1", "status": "pending"}]}
    fake = _C(st)
    assert review(fake, "p1", "granskare", "accepted", "Bra pass")["status"] == "accepted"
    try:
        review(fake, "p1", "granskare", "rejected", "")
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("ett granskat förslag får inte granskas om")


def test_bara_granskare_ser_forslagen(monkeypatch):
    import trixa_api.ui as ui
    from fastapi import HTTPException
    from starlette.requests import Request

    req = Request({"type": "http", "headers": [], "state": {}})
    monkeypatch.setattr(ui, "_current_user_id", lambda r: "vem-som-helst")
    monkeypatch.setenv("TRIXA_PASSBANK_REVIEWERS", "granskare-1")
    try:
        ui._require_reviewer(req)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("inloggad utan granskarroll fick se förslagen")
    monkeypatch.setattr(ui, "_current_user_id", lambda r: "granskare-1")
    assert ui._require_reviewer(req) == "granskare-1"


# ---------- import till YAML ----------


def test_godkant_forslag_blir_giltig_yaml_med_riktig_kod():
    from trixa_api.agent_proposals import _build_workout
    from coach.trixa import import_proposals as imp

    workout = _build_workout(_proposal(), "run")
    target = imp._target_file(workout)
    assert target is not None and target.name == "run_AE.yaml"
    existing = {"AE2_run_01", "AE2_run_05", "AE2_run_template"}
    code = imp._next_code(workout, existing)
    assert code == "AE2_run_06"
    workout["code"] = code
    block = imp._yaml_block(workout, {
        "id": "p1", "proposer_name": "Nils", "created_at": "2026-11-22",
        "reviewed_at": "2026-11-23", "rationale": "Gillad.", "evidence": {"done": 2, "sessions": 2,
                                                                           "again": {"yes": 2}},
    })
    merged = yaml.safe_load(target.read_text(encoding="utf-8").rstrip("\n") + "\n" + block)
    added = merged["workouts"][-1]
    assert added["code"] == "AE2_run_06"
    assert added["name"] == "Lugn runda med tre korta backar"
    assert "Föreslaget av Nils" in added["coach_notes"]
