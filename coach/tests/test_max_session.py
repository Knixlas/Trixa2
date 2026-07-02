"""Tester för per-period max_session_minutes (2026-07-02)."""

from coach.engine.workouts import max_session_minutes


def test_plain_int_phases_unchanged():
    assert max_session_minutes("prep", "run") == 60
    assert max_session_minutes("build", "bike") == 300


def test_base_is_per_period():
    assert max_session_minutes("base", "bike", "base_1") == 150
    assert max_session_minutes("base", "bike", "base_2") == 210
    assert max_session_minutes("base", "bike", "base_3") == 270
    assert max_session_minutes("base", "run", "base_3") == 120


def test_dict_without_period_returns_max():
    # Konservativt tak när perioden är okänd
    assert max_session_minutes("base", "bike") == 270


def test_unspecified_returns_none():
    assert max_session_minutes("race", "run") is None
    assert max_session_minutes("base", "swim") is None
