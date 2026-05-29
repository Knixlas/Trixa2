"""CLI: validera passbanken och rendera exempel.

Använd som:
    python -m coach.engine.passbank.verify_and_render

Eller direkt:
    python coach/engine/passbank/verify_and_render.py

Skriver RENDERED_EXAMPLES.md till coach/data/workouts/.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .loader import (
    AthleteProfile,
    DEFAULT_WORKOUT_DIR,
    discover_workout_files,
    load_drills,
    load_workouts,
)
from .profile import load_profile
from .renderer import render_workout
from .templates import resolve_template
from .validator import ValidationError, validate_passbank


# Exempel-pass att rendera per disciplin. Justera dessa om passbanken växer.
EXAMPLE_CODES = {
    "swim": ["AE2_swim_01", "TE1_swim_01", "ME1_swim_01"],
    "bike": ["AE1_bike_template", "AE2_bike_01", "AE2_bike_02"],
    "run": ["AE1_run_template", "AE2_run_01"],
}


def _summary(workouts: list[dict], drills: list[dict]) -> None:
    """Skriv ut sammanfattning över banken."""
    print(f"Laddade {len(workouts)} pass och {len(drills)} drills.\n")

    by_disc: dict[str, int] = {}
    by_cat: dict[str, list[str]] = {}
    for w in workouts:
        d = w.get("discipline", "?")
        by_disc[d] = by_disc.get(d, 0) + 1
        cat = w.get("category", "?")
        by_cat.setdefault(cat, []).append(w.get("code", "?"))

    print("Pass per disciplin:")
    for disc in sorted(by_disc):
        print(f"  {disc}: {by_disc[disc]}")

    print("\nPass per kategori:")
    for cat in sorted(by_cat):
        print(f"  {cat}: {len(by_cat[cat])}")

    by_drill_cat: dict[str, int] = {}
    for d in drills:
        by_drill_cat[d.get("category", "?")] = by_drill_cat.get(d.get("category", "?"), 0) + 1
    if by_drill_cat:
        print("\nDrills per kategori:")
        for cat in sorted(by_drill_cat):
            print(f"  {cat}: {by_drill_cat[cat]}")


def _find_workout(workouts: list[dict], code: str) -> dict | None:
    for w in workouts:
        if w.get("code") == code:
            return w
    return None


def _render_examples(
    workouts: list[dict],
    drill_map: dict[str, dict],
    profile: AthleteProfile,
    output_dir: Path,
) -> Path:
    """Rendera exempel-pass per disciplin och skriv till markdown-fil."""
    out: list[str] = [
        "# Render-exempel",
        "",
        "Genererat av `coach/engine/passbank/verify_and_render.py`.",
        f"Testprofil: CSS {profile.css_sec_per_100m}s/100m, "
        f"FTP {profile.ftp_watts}W, "
        f"LTHR-bike {profile.lthr_bike_bpm}, "
        f"threshold-run {profile.threshold_pace_sec_per_km}s/km, "
        f"AT-run {profile.at_hr_run_bpm}.",
        "",
        "---",
        "",
    ]

    for disc, codes in EXAMPLE_CODES.items():
        out.append(f"# {disc.upper()}")
        out.append("")
        for code in codes:
            w = _find_workout(workouts, code)
            if w is None:
                out.append(f"_(Pass {code} hittades inte — hoppar över)_")
                out.append("")
                continue
            # Resolva templates innan rendering så konkreta värden visas
            resolved = resolve_template(w) if w.get("parameterized") else w
            out.append(render_workout(resolved, profile, drill_map))
            out.append("---")
            out.append("")

    path = output_dir / "RENDERED_EXAMPLES.md"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def main(workout_dir: Path | None = None) -> int:
    workout_dir = workout_dir or DEFAULT_WORKOUT_DIR
    print(f"Workout-katalog: {workout_dir}\n")

    files = discover_workout_files(workout_dir)
    for disc, paths in files.items():
        if paths:
            print(f"  {disc}: {len(paths)} filer ({', '.join(p.name for p in paths)})")
    print()

    workouts = load_workouts(workout_dir)
    drills = load_drills(workout_dir)

    _summary(workouts, drills)

    print("\nValiderar...")
    try:
        validate_passbank(workouts, drills)
    except ValidationError as exc:
        print("\n✗ VALIDERING MISSLYCKADES")
        print(exc)
        return 1
    print("✓ Validering OK")

    drill_map = {d["code"]: d for d in drills}

    print("\nLaddar adept-profil...")
    profile = load_profile(verbose=True)

    print("\nRenderar exempel...")
    try:
        path = _render_examples(workouts, drill_map, profile, workout_dir)
        print(f"✓ Skrev {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Render-fel: {exc}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
