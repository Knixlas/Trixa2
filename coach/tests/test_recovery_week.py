"""Tester för vilovecko-räknaren och volymskalningen (2026-07-02)."""

from datetime import date

from coach.trixa.planner import _apply_week_volume_scaling, _resolve_period_position


WEEK = date(2026, 7, 6)


def test_default_ratio_gives_4_week_cycle():
    athlete = {"phase_state": {}}
    week, length, in_phase = _resolve_period_position(athlete, "base", WEEK)
    assert (week, length, in_phase) == (1, 4, 1)


def test_masters_ratio_gives_3_week_cycle():
    athlete = {"recovery_week_ratio": "2:1", "phase_state": {}}
    week, length, in_phase = _resolve_period_position(athlete, "base", WEEK)
    assert (week, length) == (1, 3)


def test_counter_advances_within_phase():
    athlete = {"phase_state": {"current_phase": "base", "weeks_in_phase": 3}}
    week, length, in_phase = _resolve_period_position(athlete, "base", WEEK)
    assert (week, length, in_phase) == (4, 4, 4)  # vecka 4 av 4 = vilovecka


def test_counter_resets_on_phase_change():
    athlete = {"phase_state": {"current_phase": "base", "weeks_in_phase": 7}}
    week, length, in_phase = _resolve_period_position(athlete, "build", WEEK)
    assert (week, in_phase) == (1, 1)


def test_rerun_same_week_does_not_increment():
    athlete = {"phase_state": {
        "current_phase": "base", "weeks_in_phase": 2,
        "last_planned_week_start": WEEK.isoformat(),
    }}
    week, length, in_phase = _resolve_period_position(athlete, "base", WEEK)
    assert in_phase == 2  # inte 3 — samma vecka re-körd


def test_explicit_override_wins():
    athlete = {"phase_state": {"current_phase": "base", "weeks_in_phase": 1}}
    week, length, _ = _resolve_period_position(
        athlete, "base", WEEK, override_week=4, override_len=4
    )
    assert (week, length) == (4, 4)


def test_recovery_week_scales_volume():
    decisions = {"discipline_hours": {"run": 3.0, "bike": 4.0, "swim": 2.0}}
    out = _apply_week_volume_scaling(decisions, "base", 4, 4, 4)
    assert out["recovery_week"]["active"] is True
    assert out["discipline_hours"]["bike"] == 2.4  # 60%


def test_normal_week_not_scaled():
    decisions = {"discipline_hours": {"run": 3.0}}
    out = _apply_week_volume_scaling(decisions, "base", 2, 4, 2)
    assert out["recovery_week"]["active"] is False
    assert out["discipline_hours"]["run"] == 3.0


def test_peak_taper_scales_progressively():
    d1 = _apply_week_volume_scaling({"discipline_hours": {"bike": 4.0}}, "peak", 1, 4, 1)
    d2 = _apply_week_volume_scaling({"discipline_hours": {"bike": 4.0}}, "peak", 2, 4, 2)
    assert d1["taper"]["factor"] == 0.75
    assert abs(d2["taper"]["factor"] - 0.5625) < 1e-3  # 0.75^2 (loggas rundad)
    assert d2["discipline_hours"]["bike"] < d1["discipline_hours"]["bike"]
