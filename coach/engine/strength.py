"""Logik för styrketräning per träningsfas (källa: data/strength.yaml).

Hanterar den speciella regeln för base_1 där MT används första halvan
och MS andra halvan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ._loader import load_yaml


PhaseCode = Literal["prep", "base", "build", "peak", "race", "transition"]
ProtocolCode = Literal["AA", "MT", "MS", "SM", "light_maintenance"]


@dataclass(frozen=True)
class StrengthProtocol:
    """Aktuellt styrkeprotokoll för en given vecka."""

    protocol_code: ProtocolCode
    protocol_name: str
    goal: str
    intensity: str
    reps: tuple[int, int]
    sets: tuple[int, int]
    focus: str
    exercise_groups: dict[str, list[str]]
    note: str | None = None


def current_strength_protocol(
    phase: PhaseCode,
    period: str | None = None,
    week_in_period: int | None = None,
    weeks_in_period: int | None = None,
) -> StrengthProtocol:
    """Returnera aktuellt styrkeprotokoll för en given vecka.

    Specialfall:
    - base_1 har MT första halvan, MS andra halvan
    - Övriga base-perioder har MS
    - Övriga faser har ett fast protokoll

    Args:
        phase: fas-kod
        period: t.ex. "base_1" (krävs för base)
        week_in_period: 1-indexerad
        weeks_in_period: totalt antal veckor i perioden
    """
    strength = load_yaml("strength.yaml")
    phase_data = strength["strength_by_phase"].get(phase)
    if phase_data is None:
        raise ValueError(f"Inget styrkeprotokoll definierat för fas: {phase}")

    protocol_code = _resolve_protocol_code(
        phase, phase_data, period, week_in_period, weeks_in_period
    )

    return StrengthProtocol(
        protocol_code=protocol_code,
        protocol_name=strength["protocol_types"].get(protocol_code, protocol_code),
        goal=phase_data.get("goal", ""),
        intensity=phase_data.get("intensity", ""),
        reps=tuple(phase_data.get("reps", [0, 0])),
        sets=tuple(phase_data.get("sets", [0, 0])),
        focus=phase_data.get("focus", ""),
        exercise_groups=phase_data.get("exercises", {}),
        note=phase_data.get("note"),
    )


def _resolve_protocol_code(
    phase: PhaseCode,
    phase_data: dict,
    period: str | None,
    week_in_period: int | None,
    weeks_in_period: int | None,
) -> ProtocolCode:
    """Plocka ut rätt protokollkod beroende på fas och period."""
    # Enkla fall: protokollet är en plain sträng på fasen
    if "protocol" in phase_data:
        return phase_data["protocol"]

    # Komplex fall: base har protocol_by_period
    if "protocol_by_period" in phase_data:
        if period is None:
            raise ValueError(
                f"Fas {phase} kräver att period anges för att bestämma styrkeprotokoll"
            )
        period_protocol = phase_data["protocol_by_period"].get(period)
        if period_protocol is None:
            raise ValueError(f"Okänd period för fas {phase}: {period}")

        # Om perioden har half-split (base_1: MT första halvan, MS andra halvan)
        if isinstance(period_protocol, dict):
            if week_in_period is None or weeks_in_period is None:
                raise ValueError(
                    f"Period {period} kräver week_in_period och weeks_in_period "
                    "för att avgöra första/andra halvan"
                )
            # Vid udda antal veckor blir första halvan en vecka kortare.
            # Ex: 4v → 1-2 första, 3-4 andra. 5v → 1-2 första, 3-5 andra. 6v → 1-3, 4-6.
            first_half_end = weeks_in_period // 2
            if week_in_period <= first_half_end:
                return period_protocol["first_half"]
            return period_protocol["second_half"]

        return period_protocol

    raise ValueError(f"Kunde inte bestämma styrkeprotokoll för fas: {phase}")
