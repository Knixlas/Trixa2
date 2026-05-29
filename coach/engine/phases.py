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
    optimal_phase: PhaseCode | None = None  # vad en idealisk periodisering vore
    behind: bool = False  # True om phase capats under optimal (ligger efter)


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


# ---------- Optimal-plan-modell ----------
#
# Top-down: optimal fas räknas bakåt från race-datum, sedan capas den av vad
# adeptens faktiska volym/hälsa bär. De flesta adepter (särskilt externa) kommer
# in MITT i säsongen — då är "börja i prep och avancera uppåt" fel. Allt
# deterministiskt, ingen LLM.

_PHASE_ORDER: list[PhaseCode] = ["prep", "base", "build", "peak", "race", "transition"]


def _phase_rank(phase: PhaseCode) -> int:
    return _PHASE_ORDER.index(phase)


def _optimal_phase_for_race(weeks_until_race: int | None) -> PhaseCode:
    """Fasen en idealisk periodisering vore i, givet veckor till tävling.

    Räknat bakåt från loppet med fas-längderna (min) i phases.yaml. Utan
    tävling → base (generell grundträning), capas sedan av readiness.
    """
    if weeks_until_race is None:
        return "base"
    w = weeks_until_race
    if w <= 2:
        return "race"
    if w <= 4:
        return "peak"
    if w <= 12:
        return "build"
    if w <= 24:
        return "base"
    return "prep"


def _sustainable_phase(state: AthleteState) -> PhaseCode:
    """Högsta uthållighetsfas som adeptens faktiska volym/hälsa bär just nu."""
    phases = load_yaml("phases.yaml")["phases"]
    if state.has_injury or state.has_overtraining_signs:
        return "prep"  # backa av vid skada/överträning
    hours = state.weekly_training_hours
    build_min = phases["build"]["entry_criteria"]["min_weekly_hours"]
    base_min = phases["base"]["entry_criteria"]["min_weekly_hours"]
    if hours >= build_min:
        return "build"
    if hours >= base_min:
        return "base"
    return "prep"


def _weeks_str(state: AthleteState) -> str:
    w = state.weeks_until_next_race
    return f"{w} v till tävling" if w is not None else "ingen tävling satt"


def determine_phase(state: AthleteState) -> PhaseRecommendation:
    """Rekommendera fas: optimal-givet-race capad av faktisk readiness.

    Deterministisk beslutsordning:
    1. Nyligen genomförd tävling → transition.
    2. Optimal fas = räkna bakåt från race-datum (race/peak/build/base/prep).
    3. Tajming-svans (peak/race): tapra mot loppet oavsett volym; skada/OT → transition.
    4. Uthållighetsregion (prep/base/build): capa optimal till vad volymen bär.
       Ligger nuläget under optimalt → behind=True (planen anpassas nedåt, och
       avvikelsen exponeras för coach/Nils).
    """
    # 1. Nyligen genomförd tävling
    if (
        state.last_race_completed_within_days is not None
        and state.last_race_completed_within_days <= 14
    ):
        return PhaseRecommendation(
            phase="transition", period=None, optimal_phase="transition",
            reason="Tävling nyligen genomförd — återhämtning prioriterad",
        )

    optimal = _optimal_phase_for_race(state.weeks_until_next_race)

    # 2/3. Tajming-driven svans — tapra mot loppet oavsett volym
    if optimal == "race":
        if state.has_injury:
            return PhaseRecommendation(
                phase="transition", period=None, optimal_phase="race", behind=True,
                reason="Skada nära tävling — skadeprevention över prestation",
            )
        return PhaseRecommendation(
            phase="race", period=None, optimal_phase="race",
            reason=f"Tävlingen är {state.weeks_until_next_race} v bort",
        )
    if optimal == "peak":
        if state.has_injury or state.has_overtraining_signs:
            return PhaseRecommendation(
                phase="transition", period=None, optimal_phase="peak", behind=True,
                reason="Tecken på överträning eller skada inför toppning",
            )
        return PhaseRecommendation(
            phase="peak", period=None, optimal_phase="peak",
            reason=f"Tävlingen är {state.weeks_until_next_race} v bort — toppningsfas",
        )

    # 4. Uthållighetsregion: capa optimal till vad volymen/hälsan bär
    sustainable = _sustainable_phase(state)
    if _phase_rank(optimal) <= _phase_rank(sustainable):
        phase: PhaseCode = optimal
    else:
        phase = sustainable
    behind = _phase_rank(phase) < _phase_rank(optimal)
    period = _first_period(phase)

    if behind:
        phases = load_yaml("phases.yaml")["phases"]
        unmet: list[str] = []
        if state.has_injury:
            unmet.append("Skada närvarande")
        if state.has_overtraining_signs:
            unmet.append("Tecken på överträning")
        opt_min = phases.get(optimal, {}).get("entry_criteria", {}).get("min_weekly_hours")
        if opt_min is not None and state.weekly_training_hours < opt_min:
            unmet.append(
                f"För låg volym för {optimal}: "
                f"{state.weekly_training_hours:.1f} < {opt_min} h/v"
            )
        return PhaseRecommendation(
            phase=phase, period=period, optimal_phase=optimal, behind=True,
            unmet_criteria=unmet,
            reason=(
                f"Optimalt vore {optimal} ({_weeks_str(state)}), men nuläget bär "
                f"{phase} — planen anpassas nedåt."
            ),
        )

    return PhaseRecommendation(
        phase=phase, period=period, optimal_phase=optimal,
        reason=f"Optimal fas {phase} givet {_weeks_str(state)}; volymen bär den.",
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
