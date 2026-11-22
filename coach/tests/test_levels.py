"""Nivåprofiler, veckobudget och loppdag (docs/13).

En nybörjare som angav 0,5 h/vecka med ett 10 km-lopp som mål fick fem
löppass och 5–6 h i veckan, samma pass som en van löpare, och veckan före
loppet 75, 80 och 75 min utan loppet i planen.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C  # noqa: E402
from coach.trixa import levels  # noqa: E402

TODAY = date(2026, 11, 22)
MONDAY = date(2026, 11, 23)


def _store(level="beginner", hours=6, sports=("run",), race_in_weeks=38,
           distance="10k", phase_state=None, rest=None):
    race_date = (MONDAY + timedelta(weeks=race_in_weeks, days=5)).isoformat()
    return {
        "athlete_profiles": [{
            "id": "ap-1", "user_id": UID, "goal": "first_race",
            "experience_level": level, "weekly_hours": hours, "sports": list(sports),
            "recovery_week_ratio": "3:1", "race_date": race_date,
            "phase_state": phase_state or {}, "onboarded_at": "2026-11-22T00:00:00Z",
            "preferred_rest_days": rest or [], "equipment": {}, "preferred_settings": {},
            "active_concerns": [],
        }],
        "races": [{"athlete_id": "ap-1", "date": race_date, "priority": "A",
                   "name": "Milen", "distance": distance}],
        "coach_overrides": [], "weekly_reports": [], "planned_sessions": [],
        "training_log": [], "daily_metrics": [], "coach_alerts": [],
    }


def _plan(monkeypatch, st, monday=MONDAY, today=TODAY, dry_run=True):
    from coach.trixa import planner

    monkeypatch.setattr(planner, "get_supabase", lambda: _C(st))
    monkeypatch.setenv("TRIXA_PUSH_TO_TP", "0")
    return planner.generate_week(UID, monday, dry_run=dry_run, today=today)


def _training(plan):
    return [w for w in plan.workouts if w.sport != "rest" and w.category != "RACE"]


# ---------- Nybörjaren ----------


def test_nyborjaren_far_tre_korta_ga_jogg_pass_forsta_veckan(monkeypatch):
    plan = _plan(monkeypatch, _store())
    runs = _training(plan)
    assert len(runs) == 3
    assert all(w.code == "AE1_run_gajogg" for w in runs)
    assert sum(w.duration_minutes for w in runs) <= 80
    assert max(w.duration_minutes for w in runs) <= 30
    assert "1 min lugn jogg, 1,5 min gång" in runs[0].details_markdown
    assert plan.engine_decisions["ramp"]["budget_minutes"] == 75


def test_nyborjaren_far_inga_test_troskel_eller_intervaller(monkeypatch):
    plan = _plan(monkeypatch, _store())
    assert set(plan.engine_decisions["categories"]) <= {"AE"}


def test_angivna_timmar_ar_ett_tak_inte_ett_golv(monkeypatch):
    st = _store(hours=0.5, sports=("run", "strength"))
    plan = _plan(monkeypatch, st)
    training = _training(plan)
    # 30 min räcker till ett gå/jogg-pass; styrkan får inte spräcka taket.
    assert sum(w.duration_minutes for w in training) <= 30
    assert [w.sport for w in training] == ["run"]
    assert any("0,5 timmar" in w for w in plan.warnings)


def test_budgeten_vaxer_tio_procent_nar_veckan_blev_gjord(monkeypatch):
    st = _store()
    _plan(monkeypatch, st, dry_run=False)
    ramp = st["athlete_profiles"][0]["phase_state"]["ramp"]
    sched = ramp["scheduled_minutes"]
    # Adepten gör hela veckan.
    for w in st["planned_sessions"]:
        if w["sport"] != "Vila":
            st["training_log"].append({"user_id": UID, "date": w["date"], "sport": w["sport"],
                                       "duration_min": w["duration_min"], "source": "manual"})
    nxt = MONDAY + timedelta(weeks=1)
    plan2 = _plan(monkeypatch, st, monday=nxt, today=nxt - timedelta(days=1))
    assert plan2.engine_decisions["ramp"]["budget_minutes"] == round(min(75, sched) * 1.10)
    assert plan2.engine_decisions["ramp"]["progress_weeks"] == 1


def test_omkorning_samma_vecka_vaxer_inte(monkeypatch):
    st = _store()
    _plan(monkeypatch, st, dry_run=False)
    first = st["athlete_profiles"][0]["phase_state"]["ramp"]["budget_minutes"]
    _plan(monkeypatch, st, dry_run=False)
    assert st["athlete_profiles"][0]["phase_state"]["ramp"]["budget_minutes"] == first


def test_budgeten_sanks_nar_lite_blev_gjort():
    prof = levels.profile({"experience_level": "beginner"})
    state = {"week_start": "2026-11-16", "budget_minutes": 100, "scheduled_minutes": 100,
             "progress_weeks": 3}
    out = levels.weekly_budget(prof, state, MONDAY, 6, 30, None)
    assert out["budget_minutes"] == 90
    assert out["progress_weeks"] == 3


# ---------- Loppet ----------


def test_loppet_ligger_i_planen_med_vila_fore_och_efter(monkeypatch):
    st = _store(race_in_weeks=0)          # lördag samma vecka
    plan = _plan(monkeypatch, st)
    race_date = MONDAY + timedelta(days=5)
    by_date = {w.date: w for w in plan.workouts}
    assert by_date[race_date].category == "RACE"
    assert by_date[race_date].title == "Loppet: Milen"
    assert by_date[race_date - timedelta(days=1)].sport == "rest"
    assert by_date[race_date + timedelta(days=1)].sport == "rest"


def test_loppveckan_trappas_ner_for_alla_nivaer(monkeypatch):
    for level in ("beginner", "advanced"):
        st_far = _store(level=level, race_in_weeks=10, hours=7)
        st_race = _store(level=level, race_in_weeks=0, hours=7)
        far = sum(w.duration_minutes for w in _training(_plan(monkeypatch, st_far)))
        race = _plan(monkeypatch, st_race)
        assert race.engine_decisions["race_taper"]["factor"] == 0.45
        assert sum(w.duration_minutes for w in _training(race)) < far, level


# ---------- Advanced/elit oförändrade ----------


def test_advanced_behaller_fem_pass_och_far_inte_nyborjarpass(monkeypatch):
    plan = _plan(monkeypatch, _store(level="advanced", hours=7, race_in_weeks=20))
    training = _training(plan)
    assert len(training) >= 5
    assert not any(w.code == "AE1_run_gajogg" for w in training)
    assert plan.total_hours_target >= 5.0
    assert "ramp" not in plan.engine_decisions


def test_okand_niva_behandlas_som_idag():
    assert levels.profile({"experience_level": None})["model"] == "declared"


# ---------- Budgetanpassningen ----------


def test_stegringar_skalas_inte_som_om_de_hade_en_langd():
    from coach.trixa import planner
    from coach.engine.loader import load_workouts

    pool = {w["code"]: w for w in load_workouts()}
    strides = planner._scheduled_from_workout(pool["SS1_run_template"], MONDAY)
    long_run = planner._scheduled_from_workout(pool["AE2_run_template"], MONDAY + timedelta(days=6),
                                               is_long=True)
    week = [strides, long_run]
    planner._fit_week_to_budget(week, 60, max_sessions=None, min_session_min=15,
                                caps={}, shrink_only=True)
    assert week[0].duration_minutes == strides.duration_minutes
    assert week[1].duration_minutes < long_run.duration_minutes
