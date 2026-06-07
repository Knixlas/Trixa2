"""Validera passbankens integritet.

Kontrollerar:
- Obligatoriska fält finns
- pass-koder unika
- drill-koder unika
- drill-referenser pekar på existerande drills
- 'catchup' förekommer inte (medvetet exkluderad)
- zone-värden inom 1-5
- phase_appropriate använder godkända värden
- disciplin matchar filnamn (säkerhetsbälte)

Returnerar lista över fel snarare än att kasta vid första felet,
så hela banken kan kontrolleras på en körning.
"""

from __future__ import annotations

from typing import Any


class ValidationError(Exception):
    """Sammanslagna valideringsfel."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        msg = f"{len(errors)} valideringsfel:\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


# Obligatoriska fält. Parameterized templates får sakna total_distance_m
# men måste ha total_duration_min.
_REQUIRED_WORKOUT_FIELDS = [
    "code", "discipline", "category", "type_code", "name",
    "phase_appropriate", "intent", "main_set",
    "total_duration_min", "zone_refs", "equipment",
]

_REQUIRED_DRILL_FIELDS = [
    "code", "name", "category", "difficulty", "intent",
    "execution", "common_mistakes", "cues", "equipment",
    "typical_distance_m",
]

_VALID_DISCIPLINES = {"swim", "bike", "run", "brick", "strength"}
_VALID_CATEGORIES = {"AE", "TE", "MF", "SS", "ME", "AC", "T", "SP", "BW"}
_VALID_PHASES = {
    "prep", "base_1", "base_2", "base_3",
    "build_1", "build_2", "peak", "race", "recovery",
}


def _check_required_fields(items: list[dict], required: list[str], kind: str) -> list[str]:
    errors: list[str] = []
    for item in items:
        code = item.get("code", "?")
        for field in required:
            if field not in item:
                errors.append(f"{kind} {code!r} saknar obligatoriskt fält: {field}")
    return errors


def _check_uniqueness(items: list[dict], kind: str) -> list[str]:
    errors: list[str] = []
    seen: dict[str, int] = {}
    for item in items:
        code = item.get("code")
        if not code:
            continue
        if code in seen:
            seen[code] += 1
        else:
            seen[code] = 1
    for code, n in seen.items():
        if n > 1:
            errors.append(f"{kind}-kod {code!r} förekommer {n} gånger")
    return errors


def _check_drill_refs(workouts: list[dict], drill_codes: set[str]) -> list[str]:
    errors: list[str] = []
    for w in workouts:
        for seg in w.get("main_set", []):
            if seg.get("segment") != "drills":
                continue
            for d in seg.get("drills", []):
                if d not in drill_codes:
                    errors.append(
                        f"Pass {w.get('code', '?')!r} refererar drill {d!r} som inte finns"
                    )
                if d == "catchup":
                    errors.append(
                        f"Pass {w.get('code', '?')!r} refererar 'catchup' — medvetet exkluderad"
                    )
    return errors


def _check_enums(workouts: list[dict]) -> list[str]:
    errors: list[str] = []
    for w in workouts:
        code = w.get("code", "?")
        disc = w.get("discipline")
        if disc and disc not in _VALID_DISCIPLINES:
            errors.append(f"Pass {code!r} har okänd disciplin: {disc!r}")
        cat = w.get("category")
        if cat and cat not in _VALID_CATEGORIES:
            errors.append(f"Pass {code!r} har okänd kategori: {cat!r}")
        for phase in w.get("phase_appropriate", []):
            if phase not in _VALID_PHASES:
                errors.append(f"Pass {code!r} har okänd fas: {phase!r}")
    return errors


def _check_zones(workouts: list[dict]) -> list[str]:
    errors: list[str] = []
    for w in workouts:
        code = w.get("code", "?")
        for seg in w.get("main_set", []):
            zone = seg.get("zone")
            if zone is not None and zone not in range(1, 6):
                errors.append(f"Pass {code!r} har ogiltig zone: {zone}")
            for z in seg.get("zones_per_set", []):
                if z not in range(1, 6):
                    errors.append(f"Pass {code!r} har ogiltig zon i zones_per_set: {z}")
            for sub in seg.get("pattern", []) or []:
                z = sub.get("zone")
                if z is not None and z not in range(1, 6):
                    errors.append(f"Pass {code!r} har ogiltig zon i pattern: {z}")
    return errors


def _check_catchup_in_drills(drills: list[dict]) -> list[str]:
    """Specifik kontroll — catchup ska aldrig finnas som drill."""
    errors: list[str] = []
    for d in drills:
        if d.get("code") == "catchup":
            errors.append(
                "Drill-katalogen innehåller 'catchup' — medvetet exkluderad enligt SCHEMA.md"
            )
    return errors


def validate_passbank(workouts: list[dict], drills: list[dict]) -> None:
    """Kör alla valideringar och kasta ValidationError om något hittas.

    Vid framgång returneras None (inget värde).
    """
    errors: list[str] = []
    errors.extend(_check_required_fields(workouts, _REQUIRED_WORKOUT_FIELDS, "Pass"))
    errors.extend(_check_required_fields(drills, _REQUIRED_DRILL_FIELDS, "Drill"))
    errors.extend(_check_uniqueness(workouts, "Pass"))
    errors.extend(_check_uniqueness(drills, "Drill"))
    errors.extend(_check_enums(workouts))
    errors.extend(_check_zones(workouts))
    errors.extend(_check_catchup_in_drills(drills))

    drill_codes = {d.get("code") for d in drills if d.get("code")}
    errors.extend(_check_drill_refs(workouts, drill_codes))

    if errors:
        raise ValidationError(errors)
