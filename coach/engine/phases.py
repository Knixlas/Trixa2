"""Logik för träningsfaser (källa: data/phases.yaml + data/phase_details.yaml).

Tillhandahåller:
- determine_phase(): rekommendera fas baserat på adept-state och tävlingsdatum
- check_transition_ready(): kolla om kriterier för nästa fas är uppfyllda
- get_phase_info(): hämta fullständig fasinfo

Alla beslut är deterministiska och regelbaserade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from ._loader import load_yaml


PhaseCode = Literal["prep", "base", "build", "peak", "race", "transition"]


@dataclass(frozen=True)
class AthleteState:
    """Minsta nödvändiga state för fasbeslut. Fält som saknas tolkas konservativt."""

    weekly_training_hours: float
    has_injury: bool = False
    has_overtraining_signs: bool = False
    weeks_until_next_race: int | None = None
    last_race_completed_within_days: int | None = None
    current_phase: PhaseCode | None = None
    weeks_in_current_phase: int | None = None
    athlete_feels_rested: bool = False
    has_high_specific_fitness: bool = False


@dataclass(frozen=True)
class PhaseRecommendation:
    """Resultat från determine_phase()."""

    phase: PhaseCode
    period: str | None  # t.ex. "base_2", None om fasen saknar perioder
    reason: str
    unmet_criteria: list[str] = field(default_factory=list)


# ---------- Publika funktioner ----------


def get_phase_info(phase: PhaseCode) -> dict[str, Any]:
    """Returnera alla fält för en fas (sammanslagning av phases.yaml och phase_details.yaml)."""
    phases = load_yaml("phases.yaml")["phases"]
    details = load_yaml("phase_details.yaml")["phase_details"]
    if phase not in phases:
        raise ValueError(f"Okänd fas: {phase}")
    return {**phases[phase], **details.get(phase, {})}


def check_transition_ready(
    current_phase: PhaseCode,
    state: AthleteState,
) -> tuple[bool, list[str]]:
    """Kolla om adepten kan gå vidare från current_phase till nästa fas.

    Returns:
        (ok, lista_med_brister). ok=True om alla kriterier uppfyllda.
    """
    phases = load_yaml("phases.yaml")["phases"]
    current = phases[current_phase]
    next_phase_code = current.get("next_phase")
    if not next_phase_code:
        return True, []

    next_phase = phases[next_phase_code]
    criteria = next_phase.get("entry_criteria", {})
    if not criteria:
        return True, []

    unmet: list[str] = []

    min_hours = criteria.get("min_weekly_hours")
    if min_hours is not None and state.weekly_training_hours < min_hours:
        unmet.append(
            f"För få veckotimmar: {state.weekly_training_hours:.1f} < {min_hours}"
        )

    if criteria.get("no_injuries") and state.has_injury:
        unmet.append("Skada närvarande")

    if criteria.get("no_overtraining_signs") and state.has_overtraining_signs:
        unmet.append("Tecken på överträning")

    if criteria.get("athlete_feels_rested") and not state.athlete_feels_rested:
        unmet.append("Adepten känner sig inte utvilad")

    if criteria.get("high_specific_fitness") and not state.has_high_specific_fitness:
        unmet.append("Otillräcklig tävlingsspecifik kondition")

    proximity = criteria.get("race_proximity_weeks")
    if proximity is not None:
        weeks = state.weeks_until_next_race
        low, high = proximity
        if weeks is None or not (low <= weeks <= high):
            unmet.append(
                f"Tävlingen är inte inom {low}–{high} veckor (är: {weeks})"
            )

    if criteria.get("race_completed") and state.last_race_completed_within_days is None:
        unmet.append("Tävling ej genomförd")

    return len(unmet) == 0, unmet


def determine_phase(state: AthleteState) -> PhaseRecommendation:
    """Rekommendera vilken fas adepten ska vara i.

    Beslutsordning (deterministisk):
    1. Om tävling är genomförd nyligen → transition.
    2. Om tävlingen är inom 1-2 veckor → race.
    3. Om tävlingen är inom 2-4 veckor och hög specifik fitness → peak.
    4. Om låg träningstid (<5h/vecka) → prep.
    5. Om current_phase är satt och övergångskriterier är uppfyllda → nästa fas.
    6. Annars stanna i current_phase, eller fall tillbaka till prep om inget annat passar.
    """
    phases = load_yaml("phases.yaml")["phases"]

    # 1. Nyligen genomförd tävling
    if (
        state.last_race_completed_within_days is not None
        and state.last_race_completed_within_days <= 14
    ):
        return PhaseRecommendation(
            phase="transition",
            period=None,
            reason="Tävling nyligen genomförd — återhämtning prioriterad",
        )

    # 2. Tävlingsfas (1-2 veckor)
    if state.weeks_until_next_race is not None and 1 <= state.weeks_until_next_race <= 2:
        if state.has_injury:
            return PhaseRecommendation(
                phase="transition",
                period=None,
                reason="Skada nära tävling — skadeprevention över prestation",
            )
        return PhaseRecommendation(
            phase="race",
            period=None,
            reason=f"Tävlingen är {state.weeks_until_next_race} v bort",
        )

    # 3. Toppningsfas (2-4 veckor)
    if state.weeks_until_next_race is not None and 2 < state.weeks_until_next_race <= 4:
        if state.has_overtraining_signs or state.has_injury:
            return PhaseRecommendation(
                phase="transition",
                period=None,
                reason="Tecken på överträning eller skada inför toppning",
            )
        return PhaseRecommendation(
            phase="peak",
            period=None,
            reason=f"Tävlingen är {state.weeks_until_next_race} v bort — toppningsfas",
        )

    # 4. Låg träningsvolym → alltid prep
    prep_threshold = phases["prep"]["entry_rules"]["low_volume_threshold_hours"]
    if state.weekly_training_hours < prep_threshold:
        return PhaseRecommendation(
            phase="prep",
            period=None,
            reason=f"Veckotimmar {state.weekly_training_hours:.1f} < {prep_threshold}h",
        )

    # 5. Försök avancera från current_phase
    if state.current_phase:
        ready, unmet = check_transition_ready(state.current_phase, state)
        if ready:
            next_code = phases[state.current_phase].get("next_phase")
            if next_code:
                next_period = _first_period(next_code)
                return PhaseRecommendation(
                    phase=next_code,
                    period=next_period,
                    reason=f"Kriterier för övergång från {state.current_phase} uppfyllda",
                )
        # Stanna kvar
        return PhaseRecommendation(
            phase=state.current_phase,
            period=_current_period_estimate(state),
            reason="Stanna kvar — övergångskriterier ej uppfyllda",
            unmet_criteria=unmet,
        )

    # 6. Fallback: ingen fas angiven, börja från prep
    return PhaseRecommendation(
        phase="prep",
        period=None,
        reason="Ingen tidigare fas angiven — börja med Förberedelsefas",
    )


# ---------- Hjälpfunktioner ----------


def _first_period(phase: PhaseCode) -> str | None:
    """Returnera första perioden för en fas, eller None om fasen saknar perioder."""
    phases = load_yaml("phases.yaml")["phases"]
    phase_info = phases[phase]
    if not phase_info.get("has_periods"):
        return None
    periods = phase_info.get("periods", [])
    return periods[0]["code"] if periods else None


def _current_period_estimate(state: AthleteState) -> str | None:
    """Grov gissning av aktuell period baserat på veckor i fasen.

    För base: base_1 = v1-6, base_2 = v7-12, base_3 = v13+
    För build: build_1 = v1-6, build_2 = v7+
    """
    if not state.current_phase or state.weeks_in_current_phase is None:
        return _first_period(state.current_phase) if state.current_phase else None

    weeks = state.weeks_in_current_phase
    if state.current_phase == "base":
        if weeks <= 6:
            return "base_1"
        if weeks <= 12:
            return "base_2"
        return "base_3"
    if state.current_phase == "build":
        if weeks <= 6:
            return "build_1"
        return "build_2"
    return None
