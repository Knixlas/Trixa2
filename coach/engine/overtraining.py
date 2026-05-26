"""Logik för identifiering och hantering av överträning (källa: data/overtraining.yaml + 3.4).

Två steg:
1. assess_overtraining(signals) → OvertrainingAssessment
2. recommend_adjustment(assessment) → PlanAdjustment

Signaler kan komma både från subjektiva inputs (motivation, sömn) och objektiv data
från Garmin → Supabase-pipen (vilopuls, HRV).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ._loader import load_yaml


AssessmentLevel = Literal["none", "early", "moderate", "severe"]


@dataclass
class OvertrainingSignals:
    """Insamlade signaler för bedömning. Alla fält valfria.

    Objektiva (från Garmin/Supabase):
        rhr_bpm_over_baseline: vilopuls över adeptens 7-dagars baseline (bpm)
        hrv_pct_below_baseline: HRV under baseline (%, positiv siffra = nedgång)
        sleep_score_avg_7d: snitt sömnpoäng senaste 7 dagar (0-100)
        sleep_consecutive_low_days: antal dagar i rad med låg sömnpoäng
        performance_drop_pct: % under förväntad puls/watt/pace
        consecutive_high_load_weeks: antal tunga veckor utan vilovecka

    Subjektiva (från check-ins):
        motivation_low: bool
        irritability: bool
        depression_or_anxiety: bool
        muscle_fatigue_persistent: bool
        poor_recovery: bool
        injury_present: bool
    """

    rhr_bpm_over_baseline: float | None = None
    hrv_pct_below_baseline: float | None = None
    sleep_score_avg_7d: float | None = None
    sleep_consecutive_low_days: int | None = None
    readiness_score: float | None = None
    performance_drop_pct: float | None = None
    consecutive_high_load_weeks: int | None = None

    motivation_low: bool = False
    irritability: bool = False
    depression_or_anxiety: bool = False
    muscle_fatigue_persistent: bool = False
    poor_recovery: bool = False
    injury_present: bool = False


@dataclass
class OvertrainingAssessment:
    """Resultat av en bedömning."""

    level: AssessmentLevel
    label: str
    flag_count: int
    flags: list[str] = field(default_factory=list)
    severe_flags: list[str] = field(default_factory=list)  # extra-allvarliga indikatorer


@dataclass
class PlanAdjustment:
    """Rekommenderade justeringar av träningsplanen."""

    level: AssessmentLevel
    volume_reduction_pct: int
    intensity_reduction_pct: int
    extra_rest_days: int
    add_recovery_practices: list[str]
    swap_to_low_intensity: bool
    consider_medical_consultation: bool
    communication_tone: str
    example_message: str


# ---------- Publika funktioner ----------


def assess_overtraining(
    signals: OvertrainingSignals,
    custom_thresholds: dict | None = None,
) -> OvertrainingAssessment:
    """Bedöm överträningsnivå baserat på inkomna signaler.

    Räknar antal aktiva "flags" och mappar mot tröskelvärden från overtraining.yaml.

    Args:
        signals: alla mätbara och rapporterade signaler
        custom_thresholds: valfri override av tröskelvärden (per adept)

    Returns:
        OvertrainingAssessment med nivå och lista över aktiva flaggor.
    """
    config = load_yaml("overtraining.yaml")
    thresholds = config["thresholds"]
    if custom_thresholds:
        thresholds = {**thresholds, **custom_thresholds}

    flags: list[str] = []
    severe_flags: list[str] = []

    # Objektiva signaler
    if signals.rhr_bpm_over_baseline is not None:
        rhr_thr = thresholds["resting_hr"]
        if signals.rhr_bpm_over_baseline >= rhr_thr["severely_elevated_bpm_over_baseline"]:
            flags.append("vilopuls kraftigt förhöjd")
            severe_flags.append("vilopuls kraftigt förhöjd")
        elif signals.rhr_bpm_over_baseline >= rhr_thr["elevated_bpm_over_baseline"]:
            flags.append("vilopuls förhöjd")

    if signals.hrv_pct_below_baseline is not None:
        hrv_thr = thresholds["hrv"]
        if signals.hrv_pct_below_baseline >= hrv_thr["severely_decreased_pct_vs_baseline"]:
            flags.append("HRV kraftigt sänkt")
            severe_flags.append("HRV kraftigt sänkt")
        elif signals.hrv_pct_below_baseline >= hrv_thr["decreased_pct_vs_baseline"]:
            flags.append("HRV sänkt")

    if signals.sleep_score_avg_7d is not None:
        sleep_thr = thresholds["sleep"]
        if signals.sleep_score_avg_7d < sleep_thr["low_score_threshold"]:
            flags.append("låg sömnpoäng (7d snitt)")

    if (
        signals.sleep_consecutive_low_days is not None
        and signals.sleep_consecutive_low_days >= thresholds["sleep"]["consecutive_low_days_flag"]
    ):
        flags.append("flera dagar i rad med dålig sömn")

    if signals.readiness_score is not None and "readiness" in thresholds:
        readiness_thr = thresholds["readiness"]
        if signals.readiness_score < readiness_thr["severely_low_threshold"]:
            flags.append("readiness kraftigt sänkt")
            severe_flags.append("readiness kraftigt sänkt")
        elif signals.readiness_score < readiness_thr["low_threshold"]:
            flags.append("readiness sänkt")

    if signals.performance_drop_pct is not None:
        if signals.performance_drop_pct >= thresholds["performance"]["drop_pct_vs_expected"]:
            flags.append("prestation under förväntad nivå")

    if (
        signals.consecutive_high_load_weeks is not None
        and signals.consecutive_high_load_weeks > thresholds["consecutive_high_load_weeks"]
    ):
        flags.append("flera tunga veckor utan vilovecka")

    # Subjektiva signaler
    if signals.motivation_low:
        flags.append("minskad motivation")
    if signals.irritability:
        flags.append("irritabilitet")
    if signals.depression_or_anxiety:
        flags.append("depression eller ångest")
        severe_flags.append("depression eller ångest")
    if signals.muscle_fatigue_persistent:
        flags.append("ihållande muskeltrötthet")
    if signals.poor_recovery:
        flags.append("försämrad återhämtning")
    if signals.injury_present:
        flags.append("skada närvarande")
        severe_flags.append("skada närvarande")

    # Mappa flaggor till nivå
    flag_count = len(flags)
    level = _flag_count_to_level(flag_count, config["assessment_levels"])

    # Eskalera om en extra-allvarlig flagga finns och nivån är "early"
    if severe_flags and level == "early":
        level = "moderate"

    label = config["assessment_levels"][level].get("label", level)

    return OvertrainingAssessment(
        level=level,
        label=label,
        flag_count=flag_count,
        flags=flags,
        severe_flags=severe_flags,
    )


def recommend_adjustment(assessment: OvertrainingAssessment) -> PlanAdjustment | None:
    """Returnera rekommenderad planjustering, eller None om ingen åtgärd behövs."""
    if assessment.level == "none":
        return None

    config = load_yaml("overtraining.yaml")
    actions = config["actions_by_level"][assessment.level]

    return PlanAdjustment(
        level=assessment.level,
        volume_reduction_pct=actions.get("volume_reduction_pct", 0),
        intensity_reduction_pct=actions.get("intensity_reduction_pct", 0),
        extra_rest_days=actions.get("extra_rest_days", 0),
        add_recovery_practices=actions.get("add_recovery_practices", []),
        swap_to_low_intensity=actions.get("swap_to_low_intensity", False),
        consider_medical_consultation=actions.get("consider_medical_consultation", False),
        communication_tone=actions.get("communication_tone", "supportive"),
        example_message=actions.get("example_message", "").strip(),
    )


# ---------- Hjälpfunktioner ----------


def _flag_count_to_level(flag_count: int, levels_config: dict) -> AssessmentLevel:
    """Mappa antal flaggor till bedömningsnivå."""
    for level_name, bounds in levels_config.items():
        if bounds["min_flags"] <= flag_count <= bounds["max_flags"]:
            return level_name
    # Fallback: om över alla intervall, ta severe; under = none
    return "severe" if flag_count > 0 else "none"
