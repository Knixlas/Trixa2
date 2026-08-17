"""Säsongsvyns volymprojektion ska visa vilovecko-cykeln, inte en slät ramp."""

from datetime import date

from trixa_api.season import build_season_plan

TODAY = date(2026, 8, 17)
RACE = date(2027, 8, 21)


def _plan(athlete=None):
    return build_season_plan(TODAY, RACE, peak_hours=14.0, athlete=athlete)


def test_projection_has_recovery_dips():
    plan = _plan({"recovery_week_ratio": "3:1", "phase_state": {}})
    future = [w for w in plan["weeks"] if w["future"]]
    rec = [w for w in future if w["is_recovery"]]
    assert rec, "planen framåt ska innehålla viloveckor"
    for w in rec:
        prev = plan["weeks"][w["index"] - 1]
        assert w["optimal_hours"] < prev["optimal_hours"]
        assert w["phase"] in ("prep", "base", "build")


def test_cycle_length_follows_ratio():
    plan = _plan({"recovery_week_ratio": "2:1", "phase_state": {}})
    prep_rec = [
        w["index"] for w in plan["weeks"]
        if w["phase"] == "prep" and w["is_recovery"]
    ]
    assert len(prep_rec) >= 2
    # 2:1 (masters) → 3-veckorscykel inom samma fas-band
    assert all(b - a == 3 for a, b in zip(prep_rec, prep_rec[1:]))


def test_no_recovery_in_peak_or_race():
    plan = _plan()
    assert not any(
        w["is_recovery"] for w in plan["weeks"] if w["phase"] in ("peak", "race")
    )


def test_current_week_anchored_to_phase_state():
    # 52 v till race → projektionen säger prep; adepten är 2 veckor in i fasen
    # → innevarande vecka planeras som vecka 3 av 4 (3:1).
    athlete = {
        "recovery_week_ratio": "3:1",
        "phase_state": {"current_phase": "prep", "weeks_in_phase": 2},
    }
    plan = _plan(athlete)
    cur = next(w for w in plan["weeks"] if w["is_current"])
    assert cur["week_in_period"] == 3
    assert not cur["is_recovery"]


def test_maintenance_plateau_before_ideal_period():
    # 52 v till race men ideal periodisering ~27 v → underhållsplatå (45 % av
    # peak) tills ideal-perioden börjar, ingen oavbruten årslång ramp.
    plan = _plan()
    maint = [w for w in plan["weeks"] if w["is_maintenance"] and not w["is_recovery"]]
    assert len(maint) > 4
    assert len({w["optimal_hours"] for w in maint}) == 1  # platt nivå
    assert maint[0]["optimal_hours"] == round(14.0 * 0.45, 1)
    # Rampen tar vid efter platån och når kapaciteten.
    ramped = [w for w in plan["weeks"] if not w["is_maintenance"]]
    assert max(w["optimal_hours"] for w in ramped) == 14.0


def test_capacity_plateau_through_build():
    # Kapaciteten (peak-målet) är inte en enstaka toppvecka: hela build utom
    # viloveckorna ligger på maxvolym — progressionen där är intensitet.
    plan = _plan()
    build = [
        w for w in plan["weeks"]
        if w["phase"] == "build" and not w["is_recovery"]
    ]
    assert len(build) >= 4
    assert all(w["optimal_hours"] == 14.0 for w in build)
    # Base rampar upp mot kapaciteten (progression genom grundträningen).
    base = [
        w for w in plan["weeks"]
        if w["phase"] == "base" and not w["is_recovery"]
    ]
    vols = [w["optimal_hours"] for w in base]
    assert vols == sorted(vols) and vols[0] < vols[-1]


def test_transition_after_completed_race():
    # IM genomförd 2026-08-15 (2 dagar före TODAY) → de två första veckorna
    # är återhämtningsfas med låg volym, sedan återgår projektionen.
    plan = build_season_plan(
        TODAY, RACE, peak_hours=14.0, last_race_date=date(2026, 8, 15),
    )
    cur = next(w for w in plan["weeks"] if w["is_current"])
    assert cur["phase"] == "transition"
    assert cur["optimal_hours"] == round(14.0 * 0.25, 1)
    assert not cur["is_recovery"] and not cur["is_maintenance"]
    nxt = plan["weeks"][cur["index"] + 1]
    assert nxt["phase"] == "transition"
    after = plan["weeks"][cur["index"] + 2]
    assert after["phase"] != "transition"
    # Fas-bandet visar Återhämtningsfas (phases.yaml name_sv).
    assert cur["phase_label"] == "Återhämtningsfas"


def test_planned_hours_override_projection():
    iso = TODAY.isocalendar()
    plan = build_season_plan(
        TODAY, RACE, peak_hours=14.0,
        planned_by_week={(iso[0], iso[1]): 5.5},
    )
    cur = next(w for w in plan["weeks"] if w["is_current"])
    assert cur["optimal_hours"] == 5.5
    assert cur["optimal_source"] == "plan"
    assert plan["now"]["optimal_hours"] == 5.5
    # Veckor utan plan använder projektionen.
    nxt = plan["weeks"][cur["index"] + 1]
    assert nxt["optimal_source"] == "projektion"


def test_default_without_athlete_is_3_1():
    plan = _plan()
    cur = next(w for w in plan["weeks"] if w["is_current"])
    assert cur["week_in_period"] == 1  # tom phase_state → vecka 1 i cykeln
    prep_rec = [
        w["index"] for w in plan["weeks"]
        if w["phase"] == "prep" and w["is_recovery"]
    ]
    assert all(b - a == 4 for a, b in zip(prep_rec, prep_rec[1:]))
