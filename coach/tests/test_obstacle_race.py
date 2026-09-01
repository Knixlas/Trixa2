"""Tester för TX-9 ur rapporten 2026-09-01.

race_type täckte triathlon-, löp-, cykel- och simdistanser. En OCR-adept fick
välja "10 km" och beskriva det verkliga loppet i time_goal som fritext — som
ingen logik läser. Skillnaden är inte kosmetisk: ett hinderlopp kräver grepp,
drag och klättring, och bryter ner mer än löprundan på samma sträcka.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TRIXA_ALLOW_NO_AUTH", "1")

from coach.engine.phases import transition_days_for  # noqa: E402
from trixa_api.ui import (  # noqa: E402
    _RACE_DISTANCES,
    _RACE_DISTANCE_VALUES,
    _RACE_TYPES,
)

OBSTACLE = ("obstacle_sprint", "obstacle_standard", "obstacle_ultra")


def test_obstacle_distances_are_selectable_everywhere():
    for value in OBSTACLE:
        assert value in _RACE_TYPES, f"{value} saknas i settings-vallistan"
        assert value in _RACE_DISTANCE_VALUES, f"{value} saknas i onboardingen"


def test_obstacle_options_show_for_runners_and_lifters():
    requires = {v: r for v, _label, r in _RACE_DISTANCES}
    for value in OBSTACLE:
        # Hinderbana kräver löpning OCH styrka — alternativet ska synas så snart
        # någon av dem är aktiv, inte bara för triatleter.
        assert set(requires[value].split()) == {"run", "strength"}


def test_recovery_reflects_that_obstacle_racing_costs_more_than_the_run():
    # Ett hinderlopp på ~10 km lastar överkroppen excentriskt på ett sätt en
    # löprunda inte gör. Kortare återhämtning än löploppet vore fel väg.
    assert transition_days_for("obstacle_sprint") == 5
    assert transition_days_for("obstacle_standard") == 10
    assert transition_days_for("obstacle_ultra") == 21
    assert transition_days_for("obstacle_standard") > transition_days_for("10k")


def test_unknown_distance_still_falls_back_to_default():
    assert transition_days_for("mudrun") == transition_days_for(None)


def test_migration_widens_the_database_constraint():
    sql = (
        Path(__file__).resolve().parents[2]
        / "db" / "migrations" / "012_obstacle_race_distances.sql"
    ).read_text(encoding="utf-8")
    for value in OBSTACLE:
        assert f"'{value}'" in sql, f"{value} saknas i CHECK-constraintet"
    # De gamla distanserna får inte falla bort när CHECK:en skrivs om.
    for value in ("sprint", "marathon", "gran_fondo", "swim_meet", "other"):
        assert f"'{value}'" in sql


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
