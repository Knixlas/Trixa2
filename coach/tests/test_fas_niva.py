"""Fasen följer nybörjarens egen upptrappning, inte triathlonens timgränser.

Bas krävde 5 h/vecka (phases.yaml), så en nybörjare stod i "Förberedelse"
hela säsongen fram till toppningen (docs/13).
"""

from __future__ import annotations

from coach.engine.phases import AthleteState, determine_phase
from coach.trixa import levels
from coach.tests.test_levels import _plan, _store

BEGINNER = levels.profile({"experience_level": "beginner"})


def _phase(progress, weeks_to_race, hours=1.5):
    return determine_phase(AthleteState(
        weekly_training_hours=hours, weeks_until_next_race=weeks_to_race,
        sustainable_phase_override=levels.sustainable_phase(BEGINNER, progress),
    ))


def test_forsta_veckorna_ar_forberedelse():
    assert _phase(0, 20).phase == "prep"


def test_efter_fyra_gjorda_veckor_grundtraning():
    rec = _phase(4, 20)
    assert rec.phase == "base" and not rec.behind


def test_efter_tolv_gjorda_veckor_uppbyggnad_nar_loppet_narmar_sig():
    assert _phase(12, 10).phase == "build"


def test_loppet_satter_taket():
    assert _phase(30, 30).phase == "prep"     # 30 v kvar: optimalt prep


def test_bakom_ar_inte_en_timfraga_for_nyborjaren():
    rec = _phase(2, 20)
    assert rec.behind
    assert rec.unmet_criteria == ["Grunden byggs fortfarande upp i adeptens egen takt"]


def test_skada_backar_fortfarande():
    rec = determine_phase(AthleteState(
        weekly_training_hours=1.5, weeks_until_next_race=10, has_injury=True,
        sustainable_phase_override="build",
    ))
    assert rec.phase == "prep"


def test_avancerad_styrs_av_timmar_som_forut():
    assert levels.sustainable_phase(levels.profile({"experience_level": "advanced"}), 50) is None


def test_planen_for_nyborjare_med_framsteg_hamnar_i_grundtraning(monkeypatch):
    st = _store(race_in_weeks=20)
    st["athlete_profiles"][0]["phase_state"] = {
        "ramp": {"week_start": "2026-11-16", "budget_minutes": 120,
                 "scheduled_minutes": 120, "progress_weeks": 5},
    }
    plan = _plan(monkeypatch, st)
    assert plan.phase == "base"
