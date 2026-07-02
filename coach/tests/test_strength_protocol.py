"""Tester för styrkeprotokollen (protocol_parameters, 2026-07-02)."""

from coach.engine.strength import current_strength_protocol


def test_protocol_parameters_win_over_phase_block():
    ms = current_strength_protocol("base", "base_2", 3, 4)
    assert ms.protocol_code == "MS"
    assert ms.reps == (3, 6)
    assert ms.sets == (2, 3)
    assert ms.intensity == "heavy"
    assert ms.sessions_per_week == (2, 2)


def test_sm_is_maintenance_not_strength_building():
    sm = current_strength_protocol("build")
    assert sm.protocol_code == "SM"
    assert sm.sets == (1, 2)
    assert sm.reps == (6, 10)
    assert sm.sessions_per_week == (1, 1)


def test_base_1_half_split():
    # 6 veckor: first_half_end = 3 → v1-3 MT, v4-6 MS
    assert current_strength_protocol("base", "base_1", 3, 6).protocol_code == "MT"
    assert current_strength_protocol("base", "base_1", 4, 6).protocol_code == "MS"
    # 5 veckor: first_half_end = 2 → v2 MT, v3 MS
    assert current_strength_protocol("base", "base_1", 2, 5).protocol_code == "MT"
    assert current_strength_protocol("base", "base_1", 3, 5).protocol_code == "MS"


def test_all_phases_have_protocol():
    for phase in ("prep", "base", "build", "peak", "race", "transition"):
        period = "base_2" if phase == "base" else None
        p = current_strength_protocol(phase, period, 1, 4)
        assert p.protocol_code
        assert p.reps != (0, 0), f"{phase}: reps saknas — protocol_parameters täcker inte {p.protocol_code}"
        assert p.sessions_per_week is not None, f"{phase}: sessions_per_week saknas"
