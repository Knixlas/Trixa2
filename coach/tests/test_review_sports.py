"""Sportvokabulär-fynden ur kodöversynen 2026-09-02 (docs/12, avsnitt E).

Tretton oberoende översättningstabeller som motsade varandra. Nu ett
register (coach/trixa/sports.py) som alla lager läser ur.

E1  Brick kunde aldrig bli "Genomförd".
E2  MCP sparade "Cykling"/"biking" verbatim.
E3  main.py hade kvar Yoga→rest.
E4  Två normalisatorer för training_log; den döda testades.
E5  TP-synken skrev "Lopning" utan ö.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402
from coach.trixa import sports  # noqa: E402

TODAY = date(2026, 9, 2)


# ---------- registret ----------


def test_alla_stavningar_landar_pa_samma_gren():
    for v in ("Cykel", "Cykling", "bike", "biking", "Ride", "GravelRide", "cykling "):
        assert sports.canon(v) == "bike", v
    for v in ("Löpning", "Lopning", "run", "running", "VirtualRun", "Löp"):
        assert sports.canon(v) == "run", v
    assert sports.canon("Yoga") == "yoga"
    assert sports.canon("Brick") == "brick"
    assert sports.canon("Promenad") == "walk"
    assert sports.canon("Vila") == "rest"
    assert sports.canon("skidor") is None          # okänt är okänt


def test_normalize_sv_ger_ett_lagringsnamn():
    assert sports.normalize_sv("Cykling") == "Cykel"
    assert sports.normalize_sv("biking") == "Cykel"
    assert sports.normalize_sv("Lopning") == "Löpning"
    assert sports.normalize_sv("Simning") == "Sim"


def test_tp_och_strava_mappar_genom_registret():
    assert sports.from_tp_id(4) == "brick"
    assert sports.from_tp_id(13) == "walk"
    assert sports.sv(sports.from_tp_id(3)) == "Löpning"     # E5: med ö
    assert sports.from_strava("Yoga") == "yoga"
    assert sports.tp_name("bike") == "Bike"


def test_promenad_bedoms_som_vila_men_ar_ingen_vilodag():
    assert sports.status_kind("walk") == "rest"
    assert sports.is_rest("walk") is False
    assert sports.is_training("walk") is False
    assert sports.status_kind("yoga") == "training"


# ---------- E1: brick ----------


def test_brick_loggat_som_ett_tp_pass_ar_genomfort():
    import trixa_api.ui as ui

    brick = ui._normalize_log_activity({"date": "2026-09-01", "sport": "Brick",
                                        "title": "BAE1", "duration_min": 120, "source": "tp"})
    assert brick["_sport"] == "brick"
    status = ui._compute_status("2026-09-01", "brick", "BAE1_brick_01", 120, [brick], TODAY)
    assert status["key"] == "done"
    # och raden överlever städningen inför säsongsvyn
    kept = ui._clean_log_rows([{"date": "2026-09-01", "sport": "Brick", "duration_min": 120,
                                "source": "tp"}])
    assert len(kept) == 1


# ---------- E2: MCP sparar kanoniskt ----------


def _agent_client():
    st = {"api_tokens": [], "profiles": [{"id": UID}],
          "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
          "planned_sessions": [], "exercise_logs": []}
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


def test_plan_session_normaliserar_grenen():
    c, st, aa = _agent_client()
    for given in ("Cykling", "biking", "bike"):
        r = c.post("/agent/plan/session", headers=_with_token(st, aa), json={
            "date": "2026-09-10", "sport": given, "title": "Rull"})
        assert r.status_code == 200, r.text
        assert r.json()["sport"] == "Cykel"
    assert {row["sport"] for row in st["planned_sessions"]} == {"Cykel"}
    assert len(st["planned_sessions"]) == 1        # upsert på samma kanoniska nyckel


def test_okand_gren_avvisas_i_stallet_for_att_kapitaliseras():
    c, st, aa = _agent_client()
    r = c.post("/agent/plan/session", headers=_with_token(st, aa), json={
        "date": "2026-09-10", "sport": "skidor", "title": "Längd"})
    assert r.status_code == 400
    assert st["planned_sessions"] == []


# ---------- E3: main.py ----------


def test_api_week_current_kanner_yoga():
    import trixa_api.main as main

    assert main._SV_EN_SPORT.get("Yoga") == "yoga"
    assert main._SV_EN_SPORT.get("Vila") == "rest"


# ---------- E4: en normalisator ----------


def test_den_doda_normalisatorn_ar_borta():
    import trixa_api.ui as ui

    for name in ("_normalize_training_log_activity", "_fetch_week_activities",
                 "_fetch_activities_range", "_actual_hours_by_week", "_decorate_timeline"):
        assert not hasattr(ui, name), name


def test_virtualrun_matchar_planerad_lopning():
    import trixa_api.ui as ui

    act = ui._normalize_log_activity({"date": "2026-09-01", "sport": "VirtualRun",
                                      "duration_min": 40, "source": "strava"})
    assert act["_sport"] == "run"
