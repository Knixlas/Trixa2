"""Läs YAML-filer från coach/data/workouts/.

Hanterar:
- Auto-discovery av disciplin-filer (swim_*, bike_*, run_*, strength_*)
- Drill-katalog (swim_drills.yaml — bara sim har drills idag)
- AthleteProfile-dataclass för zonberäkningar
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


# Default-katalog: coach/data/workouts/ — syskon till engine/.
# Från coach/engine/loader.py: parent = engine/, parent.parent = coach/.
DEFAULT_WORKOUT_DIR = (
    Path(__file__).resolve().parent.parent / "data" / "workouts"
)


@dataclass(frozen=True)
class AthleteProfile:
    """Minimal adept-profil för zonberäkningar.

    Alla värden är valfria — renderaren faller tillbaka till
    segmentbeskrivning utan konkreta värden om data saknas för en
    disciplin.

    Fält:
        css_sec_per_100m: Critical Swim Speed (för sim-pace-zoner)
        ftp_watts: Functional Threshold Power (för bike-watt-zoner)
        lthr_bike_bpm: Lactate Threshold HR för cykel (för bike-puls-zoner)
        threshold_pace_sec_per_km: Tröskel-pace för löpning
        at_hr_run_bpm: Anaerobic Threshold HR för löpning
        max_hr_bpm: Max-puls (sekundär referens)
    """

    css_sec_per_100m: float | None = None
    ftp_watts: int | None = None
    lthr_bike_bpm: int | None = None
    threshold_pace_sec_per_km: float | None = None
    at_hr_run_bpm: int | None = None
    max_hr_bpm: int | None = None


def discover_workout_files(workout_dir: Path | None = None) -> dict[str, list[Path]]:
    """Hitta alla YAML-filer per disciplin.

    Returns:
        {"swim": [Path, ...], "bike": [...], "run": [...], "strength": [...]}
    """
    workout_dir = workout_dir or DEFAULT_WORKOUT_DIR
    if not workout_dir.exists():
        raise FileNotFoundError(f"Workout-katalog saknas: {workout_dir}")

    result: dict[str, list[Path]] = {
        "swim": [],
        "bike": [],
        "run": [],
        "strength": [],
        "brick": [],
    }
    for path in sorted(workout_dir.glob("*.yaml")):
        name = path.name
        # Hoppa över drill-filer och övriga konfigfiler
        if "drill" in name or name in {"athlete_config.yaml", "races.yaml",
                                       "phases.yaml", "phase_details.yaml",
                                       "workouts.yaml", "strength.yaml",
                                       "strength_exercises.yaml",
                                       "overtraining.yaml"}:
            continue
        for disc in result:
            if name.startswith(f"{disc}_"):
                result[disc].append(path)
                break
    return result


# Passbanken är 28 YAML-filer, ~330 KB, ~250 ms att parsa. Utan cache lästes
# den om vid varje anrop — upp till fyra gånger för ett enda "byt gren"-klick
# (docs/12 H1). Nu en gång per process och katalog; anropare får en djup
# kopia så att de kan mutera (planeraren gör det) utan att förgifta cachen.


@lru_cache(maxsize=4)
def _load_workouts_cached(dir_key: str) -> tuple[dict[str, Any], ...]:
    workout_dir = Path(dir_key)
    files = discover_workout_files(workout_dir)
    workouts: list[dict[str, Any]] = []
    for disc, paths in files.items():
        for path in paths:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not data or "workouts" not in data:
                continue
            for w in data["workouts"]:
                if "parametrized" in w and "parameterized" not in w:
                    w["parameterized"] = bool(w.pop("parametrized"))
                # Sätt discipline om den saknas (säkerhetsbälte —
                # alla pass i bike_*.yaml ska ha discipline: bike)
                if "discipline" not in w:
                    w["discipline"] = disc
                workouts.append(w)
    return tuple(workouts)


@lru_cache(maxsize=4)
def _load_catalogue_cached(path_key: str, key: str) -> tuple[dict[str, Any], ...]:
    path = Path(path_key)
    if not path.exists():
        return ()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(data.get(key, [])) if data else ()


def clear_workout_cache() -> None:
    """Töm passbankscachen (tester som skriver egna YAML-filer)."""
    _load_workouts_cached.cache_clear()
    _load_catalogue_cached.cache_clear()


def load_workouts(workout_dir: Path | None = None) -> list[dict[str, Any]]:
    """Ladda alla pass från alla disciplin-filer.

    Returnerar en flat lista med pass-objekt. Disciplin finns som fält
    i varje pass-objekt, så den behövs inte som extra struktur.
    """
    dir_key = str((workout_dir or DEFAULT_WORKOUT_DIR).resolve())
    return copy.deepcopy(list(_load_workouts_cached(dir_key)))


def load_strength_exercises(
    workout_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Ladda styrkeövningskatalogen."""
    path = (workout_dir or DEFAULT_WORKOUT_DIR) / "strength_exercises.yaml"
    return copy.deepcopy(list(_load_catalogue_cached(str(path.resolve()), "exercises")))


def load_drills(workout_dir: Path | None = None) -> list[dict[str, Any]]:
    """Ladda drill-katalogen (sim-drills idag, framtida även för andra)."""
    path = (workout_dir or DEFAULT_WORKOUT_DIR) / "swim_drills.yaml"
    return copy.deepcopy(list(_load_catalogue_cached(str(path.resolve()), "drills")))
