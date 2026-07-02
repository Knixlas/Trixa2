"""Tester för brickpass-stödet (discipline=brick, 2026-07-02)."""

from coach.engine.loader import load_workouts
from coach.engine.profile import DEMO_PROFILE
from coach.engine.renderer import render_workout
from coach.trixa.session_mapping import resolve_session


def _pool():
    return {w["code"]: w for w in load_workouts()}


def test_loader_discovers_brick_workouts():
    bricks = [w for w in load_workouts() if w.get("discipline") == "brick"]
    codes = {w["code"] for w in bricks}
    assert {"BAE1_brick_01", "BME1_brick_01", "BSS2_brick_01"} <= codes
    assert all(w.get("category") == "BW" for w in bricks)


def test_brick_renders_with_per_sport_zones():
    pool = _pool()
    md = render_workout(pool["BAE1_brick_01"], DEMO_PROFILE)
    # Cykeldelen ska visa watt, löpdelen pace — och segmenten sport-märkas
    assert "Cykel — " in md
    assert "Löp — " in md
    assert "W," in md          # watt-spann från bike-zonsetet
    assert "/km" in md         # pace-spann från run-zonsetet


def test_session_mapping_resolves_brick():
    pool = _pool()
    assert resolve_session("Brick", "T2-växlingar", 75, pool)[0] == "BSS2_brick_01"
    assert resolve_session("Brick", "Lång brick", 160, pool)[0] == "BAE1_brick_01"
    assert resolve_session("Brick", "Race-simulering", 110, pool)[0] == "BME1_brick_01"


def test_race_pace_codes_reachable_via_mapping():
    pool = _pool()
    assert resolve_session("Cykel", "Race pace-block", 200, pool)[0] == "AE2_bike_04"
    assert resolve_session("Löpning", "Långpass med IM-pace", 115, pool)[0] == "AE2_run_04"
    assert resolve_session("Löpning", "Walk/run 9/1", 90, pool)[0] == "AE2_run_05"
    assert resolve_session("Sim", "Öppet vatten sighting", 55, pool)[0] == "AE2_swim_05"
    assert resolve_session("Sim", "Broken 3x1000", 90, pool)[0] == "AE2_swim_06"
