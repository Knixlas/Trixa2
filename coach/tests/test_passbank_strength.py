from coach.engine.loader import AthleteProfile, load_drills, load_strength_exercises, load_workouts
from coach.engine.renderer import render_workout
from coach.engine.templates import resolve_template
from coach.engine.validator import validate_passbank


def test_passbank_loads_strength_and_validates():
    workouts = load_workouts()
    drills = load_drills()

    validate_passbank(workouts, drills)

    strength = [w for w in workouts if w.get("discipline") == "strength"]
    assert strength
    assert all(w.get("category") == "ST" for w in strength)
    assert any(w.get("type_code") in {"AA", "MT", "MS", "SM"} for w in strength)


def test_strength_workout_renders_exercise_prescription():
    workouts = load_workouts()
    workout = next(w for w in workouts if w.get("code") == "AA_strength_01")
    resolved = resolve_template(workout) if workout.get("parameterized") else workout
    exercises = {e["code"]: e for e in load_strength_exercises()}

    rendered = render_workout(resolved, AthleteProfile(), exercise_map=exercises)

    assert "Anatomical Adaptation" in rendered
    assert "RIR" in rendered
    assert "Knäböj med kroppsvikt" in rendered
