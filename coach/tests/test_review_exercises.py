"""Övnings-/progressionsfynden ur kodöversynen 2026-09-02 (docs/12, avsnitt D).

D1  Coach-övertag av motorrad lämnade motorns steps kvar → fel övningar i
    loggformuläret, tom lista i get_week.
D2  Mallspannet sattes på steg vars reps låg utanför det (planka reps 1
    mot [12,15]) → "för tungt" varje gång.
D3  Appen härledde övningar ur steps utan katalog/spann, API:t inte alls.
D4  Avbockade övningar + annan aktivitet samma dag = "Avviken".
D5  Säsongsvyn läste aldrig exercise_logs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TRIXA_ALLOW_NO_AUTH", "1")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402
from coach.trixa.exercise_plan import exercises_from_steps, planned_exercises  # noqa: E402
from coach.trixa.strength_progression import suggest_next  # noqa: E402

TEMPLATE_RANGE = {"default": 13, "range": [12, 15]}
STEPS = [
    {"segment": "strength_block", "exercise": "bodyweight_squat",
     "prescription": {"sets": 2, "reps": 15, "rir": 4}, "load_pct": "kroppsvikt"},
    {"segment": "strength_block", "exercise": "plank",
     "prescription": {"sets": 2, "reps": 1, "rir": 4}, "load_pct": "isometrisk hålltid 30–45s"},
]


# ---------- D2 ----------


def test_mallspannet_ges_bara_till_steg_inom_spannet():
    squat, plank = exercises_from_steps(STEPS, {}, TEMPLATE_RANGE)
    assert (squat["reps_min"], squat["reps_max"]) == (12, 15)
    assert (plank["reps_min"], plank["reps_max"]) == (None, None)


def test_planka_tvingas_inte_till_for_tungt():
    plank = exercises_from_steps(STEPS, {}, TEMPLATE_RANGE)[1]
    s = suggest_next(plank, [{"session_date": "2026-09-01", "exercise_name": "Plank",
                              "sets": 2, "reps": 1, "weight_from": None, "effort": 2}])
    assert s.trend != "down"
    assert not any("för tungt" in w for w in s.warnings)


# ---------- D3 ----------


def test_planned_exercises_anvander_katalog_och_spann_for_steps_rader():
    catalogue = {"bodyweight_squat": {"name": "Knäböj med kroppsvikt"}}
    out = planned_exercises({"exercises": None, "steps": STEPS}, catalogue)
    assert out[0]["name"] == "Knäböj med kroppsvikt"


def test_planned_exercises_foredrar_exercises_kolumnen():
    out = planned_exercises({"exercises": [{"name": "X"}], "steps": STEPS})
    assert out == [{"name": "X"}]


def test_get_week_harleder_ovningar_ur_steps_som_appen():
    st = {
        "api_tokens": [], "profiles": [{"id": UID}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
        "exercise_logs": [],
        "planned_sessions": [{
            "id": "s1", "user_id": UID, "date": "2026-09-07", "sport": "Styrka",
            "title": "AA", "origin": "trixa2", "status": "planned",
            "exercises": None, "steps": STEPS,
        }],
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
    c = TestClient(app)
    r = c.get("/agent/week?monday=2026-09-07", headers=_with_token(st, aa))
    assert r.status_code == 200, r.text
    names = [e["name"] for e in r.json()["sessions"][0]["exercises"]]
    assert "Knäböj med kroppsvikt" in names


# ---------- D1 ----------


def test_coach_overtag_rensar_motorns_steps():
    st = {
        "api_tokens": [], "profiles": [{"id": UID}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
        "planned_sessions": [{
            "id": "gen-1", "user_id": UID, "date": "2026-09-03", "sport": "Styrka",
            "title": "MS1", "origin": "trixa2", "status": "planned", "steps": STEPS,
            "exercises": [{"name": "Knäböj"}], "purpose": "ST",
        }],
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
    c = TestClient(app)
    r = c.post("/agent/plan/session", headers=_with_token(st, aa), json={
        "date": "2026-09-03", "sport": "strength", "title": "Rörlighet 20 min"})
    assert r.status_code == 200, r.text
    row = st["planned_sessions"][0]
    assert row["steps"] is None and row["exercises"] is None
    assert planned_exercises(row) == []


# ---------- D4 ----------


def test_avbockade_ovningar_vinner_over_avviken():
    import trixa_api.ui as ui

    w = {"title": "Ben", "duration_minutes": 45,
         "status": {**ui._STATUS["deviated"], "key": "deviated",
                    "actual": {"summary": "Genomfört: 30 min", "duration_min": 30}}}
    ui._mark_done_from_exercise_logs(w, [{"effort": 2}, {"effort": 3}])
    assert w["status"]["key"] == "done"
    assert "2 övningar loggade" in w["status"]["actual"]["summary"]
    assert "även 30 min" in w["status"]["actual"]["summary"]


# ---------- D5 ----------


def test_sasongsvyn_raknar_avbockat_styrkepass_som_genomfort():
    import trixa_api.ui as ui
    from datetime import date, timedelta

    today = date.today()
    last_week_wed = today - timedelta(days=today.weekday() + 5)   # förra veckans onsdag
    fake = _C({
        "planned_sessions": [{"date": last_week_wed.isoformat(), "sport": "Styrka",
                              "title": "Ben", "workout_code": "", "duration_min": 45,
                              "user_id": UID}],
        "training_log": [],
        "exercise_logs": [{"user_id": UID, "session_date": last_week_wed.isoformat(),
                           "effort": 2}],
    })
    comp = ui._compliance_by_week(fake, "81b667bc", today, UID)
    iso = last_week_wed.isocalendar()
    assert comp[(iso[0], iso[1])] == "green"      # 1 av 1 gjort; förut "red"
