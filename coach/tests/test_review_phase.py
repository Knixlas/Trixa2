"""Fas/period/styrke-fynden ur kodöversynen 2026-09-02 (docs/12, avsnitt C).

C1  determine_phase returnerade alltid fasens första period — hela basfasen
    var base_1, TE/MF (bara i base_2/base_3) nåddes aldrig.
C2  Styrkans MT→MS-halvering räknade på viloveckocykeln (3–4 v) i stället
    för periodens längd → MT, MS, MS, MT, MS, MS…
C3  Fas-override applicerades efter positionsräkningen → weeks_in_phase
    nollställdes varje vecka, kategorier räknades för fel fas.
C4  Dashboard och pass-byten körde motorn med "vecka 1 av 6" hårdkodat och
    utan klient.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.engine.phases import (  # noqa: E402
    AthleteState,
    PhaseRecommendation,
    _current_period_estimate,
    determine_phase,
    period_position,
)
from coach.engine.strength import current_strength_protocol  # noqa: E402
from coach.trixa import planner  # noqa: E402


def _base_state(weeks_in_phase: int | None) -> AthleteState:
    """Adept mitt i basfasen, 20 veckor till tävling, gott om volym."""
    return AthleteState(
        weekly_training_hours=10.0, has_injury=False, has_overtraining_signs=False,
        weeks_until_next_race=20, current_phase="base",
        weeks_in_current_phase=weeks_in_phase,
    )


# ---------- C1: perioden följer veckorna i fasen ----------


def test_perioden_avancerar_med_veckorna_i_fasen():
    assert _current_period_estimate(_base_state(0)) == "base_1"     # vecka 1
    assert _current_period_estimate(_base_state(5)) == "base_1"     # vecka 6
    assert _current_period_estimate(_base_state(6)) == "base_2"     # vecka 7
    assert _current_period_estimate(_base_state(12)) == "base_3"    # vecka 13
    assert _current_period_estimate(_base_state(40)) == "base_3"    # sista tar resten


def test_determine_phase_bar_perioden_i_samma_fas():
    rec = determine_phase(_base_state(8))
    if rec.phase == "base":                     # beror på optimal-mot-tävling
        assert rec.period == "base_2"
    else:
        assert rec.period is not None or rec.phase in ("peak", "race", "transition")


def test_ny_fas_borjar_i_forsta_perioden():
    state = AthleteState(
        weekly_training_hours=10.0, has_injury=False, has_overtraining_signs=False,
        weeks_until_next_race=20, current_phase="prep", weeks_in_current_phase=9,
    )
    rec = determine_phase(state)
    if rec.phase == "base":
        assert rec.period == "base_1"


# ---------- C2: styrkan räknar på periodens position ----------


def test_period_position_ur_veckor_i_fasen():
    assert period_position("base", "base_1", 1) == (1, 6)
    assert period_position("base", "base_1", 4) == (4, 6)
    assert period_position("base", "base_2", 7) == (1, 6)
    assert period_position("base", "base_2", 11) == (5, 6)
    assert period_position("peak", None, 3) is None


def test_mt_ms_vaxlar_en_gang_genom_base_1():
    """Masters (2:1): förut MT, MS, MS, MT, MS, MS — nu MT×3, MS×3."""
    protocols = []
    for week in range(1, 7):
        w, n = period_position("base", "base_1", week)
        protocols.append(current_strength_protocol("base", "base_1", w, n).protocol_code)
    assert protocols == ["MT", "MT", "MT", "MS", "MS", "MS"]


def test_run_engine_ger_styrkan_periodposition_inte_cykelposition():
    state = _base_state(3)                      # vecka 4 i base → base_1, andra halvan
    rec = PhaseRecommendation(phase="base", period="base_1", optimal_phase="base",
                              reason="test")
    sig = planner._build_ot_signals({"phase_state": {}}, None)
    # Cykelposition 1 av 3 (första veckan i en ny 2:1-cykel) — skulle gett MT.
    out = planner._run_engine(state, sig, 1, 3, phase_rec=rec, weeks_in_phase=4)
    assert out["strength_protocol"] == "MS"
    # Utan weeks_in_phase: gamla beteendet, cykelpositionen styr.
    old = planner._run_engine(state, sig, 1, 3, phase_rec=rec)
    assert old["strength_protocol"] == "MT"


# ---------- C3: fas-override före positionen ----------


def test_fas_override_ger_konsekvent_fas_och_raknar_veckor_vidare():
    rec = PhaseRecommendation(phase="build", period="build_1", optimal_phase="build",
                              reason="motor")
    ov = {"scope": "phase", "override_decision": {"phase": "peak"}, "motivation": "taper nu"}
    new_rec, honored = planner._apply_phase_override(rec, [ov])
    assert new_rec.phase == "peak" and honored == [ov]
    assert "Override" in new_rec.reason

    # Positionsräknaren jämför lagrad fas med den EFFEKTIVA fasen: räknar upp.
    athlete = {"recovery_week_ratio": "3:1",
               "phase_state": {"current_phase": "peak", "weeks_in_phase": 2,
                               "last_planned_week_start": "2026-08-24"}}
    _, _, weeks = planner._resolve_period_position(athlete, new_rec.phase, date(2026, 8, 31))
    assert weeks == 3                            # inte tillbaka till 1


def test_apply_overrides_hoppar_fasen_nar_den_redan_applicerats():
    decisions = {"phase_recommendation": {"phase": "build", "period": "build_1"},
                 "discipline_hours": {}, "overtraining": {}, "plan_adjustment": None}
    ov = {"scope": "phase", "override_decision": {"phase": "peak"}, "motivation": "x"}
    out, honored = planner._apply_overrides(decisions, [ov], skip_phase=True)
    assert honored == []
    assert out["phase_recommendation"]["phase"] == "build"   # orörd här
