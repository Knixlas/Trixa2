"""Trixa-planner — generera en konkret veckoplan deterministiskt.

Flöde:
    fetch_athlete  →  build_state  →  run_engine  →  select_workouts
                                                    ↓
                                              schedule_workouts
                                                    ↓
                                                persist_plan

Public entry:
    generate_week(athlete_user_id, week_start, dry_run=True) -> WeekPlan

CLI:
    python -m coach.trixa.planner --athlete-user-id <uuid> --week-start YYYY-MM-DD [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from coach.trixa import clock
from coach.engine._loader import load_yaml
from coach.engine.loader import (
    AthleteProfile,
    load_drills,
    load_strength_exercises,
    load_workouts,
)
from coach.engine.overtraining import (
    OvertrainingAssessment,
    OvertrainingSignals,
    acwr_thresholds,
    assess_overtraining,
    recommend_adjustment,
)
from coach.engine.phases import (
    AthleteState,
    PhaseRecommendation,
    determine_phase,
    period_position,
    transition_days_for,
)
from coach.engine.profile import profile_from_athlete_row
from coach.engine.renderer import render_workout
from coach.engine.strength import current_strength_protocol
from coach.engine.templates import resolve_template
from coach.engine.workouts import (
    distribute_weekly_hours,
    hard_training_cap_minutes,
    max_session_minutes,
    select_workout_types,
)
from coach.engine.workouts import workout_type_decisions
from coach.trixa import origins, sports
from coach.trixa.db import get_supabase
from coach.trixa.exercise_plan import exercises_from_steps
from coach.trixa.training_log import dedup_cross_source

logger = logging.getLogger("trixa.planner")


# ---------- Datatyper ----------


@dataclass
class ScheduledWorkout:
    """Ett pass placerat på en specifik dag."""

    date: date
    sport: str  # swim/bike/run/strength/rest
    code: str  # passkod från passbanken, eller "rest" / "strength_<protocol>"
    title: str
    category: str  # AE/ME/AC/MF/SS/T/BW
    duration_minutes: int
    intensity: str  # text-beskrivning för UI: "Z2", "Z4 tröskel", etc.
    workout_data: dict | None = None  # hela passet från passbanken, resolved
    notes: str = ""
    details_markdown: str = ""  # fullständig pass-rendering (intent + main_set + zoner)


@dataclass
class WeekPlan:
    """Komplett veckoplan med fullständig spårbarhet av engine-beslut."""

    athlete_id: str  # athlete_profiles.id
    athlete_user_id: str
    week_start: date
    phase: str
    period: str | None
    week_in_period: int
    total_hours_target: float
    discipline_hours: dict[str, float]
    categories: list[str]
    strength_protocol: str
    overtraining_level: str
    overtraining_flags: list[str]
    plan_adjustment: dict | None
    workouts: list[ScheduledWorkout] = field(default_factory=list)
    engine_decisions: dict = field(default_factory=dict)
    overrides_honored: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serialiserbar representation."""
        d = asdict(self)
        d["week_start"] = self.week_start.isoformat()
        for wo in d["workouts"]:
            wo["date"] = (
                wo["date"].isoformat() if isinstance(wo["date"], date) else wo["date"]
            )
        return d


# ---------- Hämta data ----------


def _fetch_athlete(client, athlete_user_id: str) -> dict:
    res = (
        client.table("athlete_profiles")
        .select("*")
        .eq("user_id", athlete_user_id)
        .single()
        .execute()
    )
    if not res.data:
        raise ValueError(f"Ingen athlete_profiles-rad för user_id={athlete_user_id}")
    return res.data


def _resolve_activity_sources(athlete: dict) -> tuple[str | None, str | None]:
    """Nycklar för recovery-cachen och användarens training_log-rader.

    Utfört läses normalt från MASTER `training_log`. `garmin_athlete_id`
    används bara för TP-matad recovery i `garmin_coach.daily_metrics`.
    Strava-reserven används bara när adepten aktiverat den eller helt saknar
    recovery-cache.
    """
    uid = athlete.get("user_id")
    garmin_id = athlete.get("garmin_athlete_id")
    strava_uid = uid if athlete.get("use_strava") or not garmin_id else None
    return garmin_id, strava_uid


def _fetch_active_overrides(client, athlete_id: str) -> list[dict]:
    res = (
        client.table("coach_overrides")
        .select("*")
        .eq("athlete_id", athlete_id)
        .eq("is_active", True)
        .execute()
    )
    return res.data or []


def _fetch_recent_workouts(
    client, user_id: str, weeks_back: int = 4, before: date | None = None
) -> list[dict]:
    """Passhistorik från MASTER planned_sessions för variation-constraint i
    pass-val. Nyckel: user_id. (Tidigare från workouts; docs/08.)"""
    if not user_id:
        return []
    since = (clock.today() - timedelta(weeks=weeks_back)).isoformat()
    query = (
        client.table("planned_sessions")
        .select("date, sport, workout_code, intensity, status")
        .eq("user_id", user_id)
        .gte("date", since)
    )
    # Historiken slutar där veckan som planeras börjar. Utan taket hamnade
    # veckans egna koder i "nyligen kört" vid en regenerering, variations-
    # filtret uteslöt dem, och redan genomförda dagar fick andra pass.
    if before is not None:
        query = query.lt("date", before.isoformat())
    res = query.order("date", desc=True).execute()
    return [row for row in (res.data or []) if row.get("status") != "cancelled"]


def _fetch_latest_weekly_report(client, athlete_id: str) -> dict | None:
    res = (
        client.table("weekly_reports")
        .select("*")
        .eq("athlete_id", athlete_id)
        .order("week_start", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


# ---------- Mänskligt skapade pass — Nils är auktoritativ ----------
#
# Nils och adepten skriver direkt till MASTER `planned_sessions`. Motorn läser
# samma tabell och rör aldrig dagar som har en rad med origin != 'trixa2'.
# Den äldre projektionen från garmin_coach.planned_workouts är pensionerad:
# tabellen saknar säker adeptkoppling och får därför inte användas i SaaS-flödet.


def _human_planned_sessions(
    client, user_id: str, week_start: date
) -> list[dict]:
    """Mänskligt skapade planned_sessions-rader för veckan.

    Allt som INTE är origin='trixa2' räknas som mänskligt (nils, manual,
    legacy-NULL) och skyddas av grinden — motorn får aldrig dubbelskriva en
    dag en människa redan planerat, oavsett vilken väg planen kom in.
    """
    end = week_start + timedelta(days=6)
    # Inget tyst fallback här. Ett svalt fel gav [] → grinden av → motorn
    # fyllde sju dagar bredvid Nils rader och pushen gav dubbletter på
    # klockan, utan en enda varning. Kan vi inte läsa vad människan planerat
    # ska vi inte planera alls; cron loggar felet och veckan får köras om.
    res = (
        client.table("planned_sessions")
        .select("date, sport, origin, status")
        .eq("user_id", user_id)
        .gte("date", week_start.isoformat())
        .lte("date", end.isoformat())
        .execute()
    )
    return [
        row for row in (res.data or [])
        if row.get("status") != "cancelled"
        and origins.is_human(row.get("origin"))
    ]


def _mark_overrides_honored(client, honored: list[dict]) -> int:
    """Stäng honoring-loopen: markera de overrides planeraren respekterat.

    Sätter honored_by_planner=true + honored_at=now() för varje override som
    faktiskt införlivats i veckoplanen. Idempotent — att sätta flaggan igen
    är harmlöst.
    """
    if not honored:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    count = 0
    for ov in honored:
        ov_id = ov.get("id")
        if not ov_id:
            continue
        (
            client.table("coach_overrides")
            .update({"honored_by_planner": True, "honored_at": now_iso})
            .eq("id", ov_id)
            .execute()
        )
        count += 1
    return count


# ---------- TP-cache och faktisk mastervolym ----------


def _fetch_garmin_metrics(
    client, garmin_athlete_id: str, today: date, days_back: int = 28
) -> list[dict]:
    """Hämta daily_metrics från garmin_coach-schemat. Nyaste först."""
    if not garmin_athlete_id:
        return []
    start = (today - timedelta(days=days_back)).isoformat()
    try:
        res = (
            client.schema("garmin_coach")
            .table("daily_metrics")
            .select(
                "metric_date, resting_hr, hrv_last_night_ms, hrv_baseline_low,"
                " hrv_baseline_high, sleep_score, readiness_score, stress_avg,"
                " acute_load, chronic_load, load_ratio"
            )
            .eq("athlete_id", garmin_athlete_id)
            .gte("metric_date", start)
            .order("metric_date", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:  # noqa: BLE001
        # Degradera, men inte tyst: utan metrics ser motorn ingen recovery
        # alls och planerar som om allt vore grönt.
        logger.warning("Kunde inte läsa daily_metrics för %s", garmin_athlete_id, exc_info=True)
        return []


def _fetch_actual_weekly_hours(
    client, user_id: str, today: date, weeks: int = 4
) -> float | None:
    """Snitt-träningstimmar per vecka från MASTER `public.training_log` (utfört).

    training_log är den konsoliderade utfört-mastern (strava + tp + manuellt +
    chat), så en källa täcker allt. Nyckel: `user_id` (profiles.id). Ersätter
    den gamla läsningen från garmin_coach.activities (docs/08, steg 3)."""
    if not user_id:
        return None
    start = (today - timedelta(weeks=weeks)).isoformat()
    try:
        res = (
            client.table("training_log")
            .select("date,sport,duration_min")
            .eq("user_id", user_id)
            .gte("date", start)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # None betyder "ingen data" nedströms och stänger datalucke-
        # varningen — ett läsfel får inte se ut som en tom logg.
        logger.warning("Kunde inte läsa training_log för %s", user_id, exc_info=True)
        return None
    rows = res.data or []
    if not rows:
        return None
    # Samma dedup som dashboarden (coach/trixa/training_log.py). Den lokala
    # exakt-till-en-decimal-nyckeln räknade tp 60,0 + strava 60,2 som två pass
    # och blåste upp veckovolymen som styr fasberedskapen (docs/12 G4).
    total_min = sum(
        float(row.get("duration_min") or 0)
        for row in dedup_cross_source(rows)
        if sports.is_training(sports.canon(row.get("sport")))
    )
    if total_min == 0:
        return None
    return total_min / 60.0 / weeks


def _fetch_strava_weekly_hours(
    client, strava_user_id: str, today: date, weeks: int = 4
) -> float | None:
    """Snitt-träningstimmar per vecka från public.strava_activities.

    Faktisk-volym-reserv för externa adepter när training_log saknar pass.
    Strava lagrar duration_min + lokalt datum.
    """
    if not strava_user_id:
        return None
    start = (today - timedelta(weeks=weeks)).isoformat()
    try:
        res = (
            client.table("strava_activities")
            .select("duration_min")
            .eq("user_id", strava_user_id)
            .gte("date", start)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    if not res.data:
        return None
    total_min = sum((row.get("duration_min") or 0) for row in res.data)
    if total_min == 0:
        return None
    return total_min / 60.0 / weeks


# Helpers för signal-beräkning (kopior av logik från coach/engine/garmin.py
# men anpassade för postgrest-data istället för QueryFn).


def _collect_nonnull(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _compute_rhr_delta(metrics: list[dict]) -> float | None:
    """Senaste RHR minus median av baseline-pool (dag 8-28)."""
    if len(metrics) < 8:
        return None
    latest = metrics[0]
    latest_rhr = latest.get("resting_hr")
    baseline = _collect_nonnull(metrics[7:], "resting_hr")
    if latest_rhr is None or not baseline:
        return None
    return float(latest_rhr) - sorted(baseline)[len(baseline) // 2]


def _compute_hrv_pct_below(metrics: list[dict]) -> float | None:
    if not metrics:
        return None
    latest = metrics[0]
    hrv = latest.get("hrv_last_night_ms")
    baseline_low = latest.get("hrv_baseline_low")
    if hrv is None or baseline_low is None or baseline_low == 0:
        return None
    delta_pct = (float(baseline_low) - float(hrv)) / float(baseline_low) * 100.0
    return max(0.0, delta_pct)


def _compute_sleep_avg(metrics: list[dict], days: int = 7) -> float | None:
    scores = _collect_nonnull(metrics[:days], "sleep_score")
    return _avg(scores)


def _compute_sleep_low_streak(metrics: list[dict], threshold: int = 60) -> int:
    streak = 0
    for row in metrics:
        score = row.get("sleep_score")
        if score is None or score >= threshold:
            break
        streak += 1
    return streak


def _compute_consecutive_high_load_weeks(metrics: list[dict]) -> int | None:
    """Räkna sammanhängande veckor med ACWR > 1.3 (skadlig zon)."""
    if len(metrics) < 14:
        return None
    by_week: dict[tuple[int, int], list[float]] = {}
    for row in metrics:
        ratio = row.get("load_ratio")
        if ratio is None:
            continue
        d = row.get("metric_date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d[:10])
            except ValueError:
                continue
        if not isinstance(d, date):
            continue
        iso_year, iso_week, _ = d.isocalendar()
        by_week.setdefault((iso_year, iso_week), []).append(float(ratio))
    if not by_week:
        return None
    sorted_weeks = sorted(by_week.keys(), reverse=True)
    _, acwr_high = acwr_thresholds()
    streak = 0
    for wk in sorted_weeks:
        avg_ratio = sum(by_week[wk]) / len(by_week[wk])
        if avg_ratio > acwr_high:
            streak += 1
        else:
            break
    return streak


# ---------- Bygg engine-state ----------


def _has_significant_injury(athlete: dict) -> bool:
    """Aktiv concern med severity ≥ 3 eller needs_followup → injury.

    Används som "any injury at all"-flagga för engine (påverkar fas-beslut).
    För disciplin-specifik impact, se _injury_impacts_per_discipline.
    """
    for concern in athlete.get("active_concerns") or []:
        if concern.get("severity", 0) >= 3:
            return True
        if concern.get("needs_followup"):
            return True
    return False


def _injury_impacts_per_discipline(athlete: dict) -> dict[str, str]:
    """Aggregera impact_per_discipline över alla active_concerns.

    Returns:
        Dict {swim/bike/run/strength: full/partial/none}.
        "full" vinner över "partial" som vinner över "none".
        Concerns utan impact_per_discipline-fält → 'none' för alla (backward compat).
    """
    rank = {"none": 0, "partial": 1, "full": 2}
    result: dict[str, str] = {"swim": "none", "bike": "none", "run": "none", "strength": "none"}
    for concern in athlete.get("active_concerns") or []:
        impacts = concern.get("impact_per_discipline") or {}
        for sport, level in impacts.items():
            if sport in result and level in rank:
                if rank[level] > rank[result[sport]]:
                    result[sport] = level
    return result


def _profile_sports(athlete: dict) -> list[str]:
    """Adeptens aktiva grenar.

    None betyder gammal profil utan explicit val och får triathlon-default.
    Tom lista betyder att adepten aktivt valt bort alla grenar.
    """
    sports_value = athlete.get("sports")
    return ["swim", "bike", "run"] if sports_value is None else list(sports_value)


def _weeks_until_race(
    athlete: dict, today: date, client: Any | None = None
) -> int | None:
    """Veckor till nästa tävling. Primärt public.races (nästa A-race),
    fallback athlete_profiles.race_date (denormaliserad reserv)."""
    race_date_raw = None

    if client is not None:
        try:
            from coach.trixa.races import fetch_next_a_race

            race = fetch_next_a_race(client, athlete.get("id"), today)
            if race:
                race_date_raw = race.get("date")
        except Exception:  # noqa: BLE001 — races-tabell saknas/fel → fallback
            race_date_raw = None

    if not race_date_raw:
        race_date_raw = athlete.get("race_date")
    if not race_date_raw:
        return None
    try:
        race_date = date.fromisoformat(str(race_date_raw)[:10])
    except (ValueError, TypeError):
        return None
    delta_days = (race_date - today).days
    if delta_days < 0:
        return None
    return delta_days // 7


def _last_race_info(
    athlete: dict, today: date, client: Any | None = None
) -> tuple[int | None, str | None]:
    """(dagar sedan, distans) för senast genomförda race, (None, None) om inget.

    Matar engine-regeln: genomfört lopp inom distansens transition-fönster
    (phase_details.post_race_recovery_days) → transition-fas.
    """
    if client is None:
        return None, None
    try:
        from coach.trixa.races import fetch_last_race

        race = fetch_last_race(client, athlete.get("id"), today)
    except Exception:  # noqa: BLE001 — races-tabell saknas/fel → ingen signal
        return None, None
    if not race:
        return None, None
    try:
        race_date = date.fromisoformat(str(race.get("date"))[:10])
    except (ValueError, TypeError):
        return None, None
    return (today - race_date).days, race.get("distance")


# Planera alltid minst så här många timmar/vecka, oavsett faktisk historik.
MIN_PLANNED_WEEKLY_HOURS = 5.0


def _build_athlete_state(
    athlete: dict,
    weekly_report: dict | None,
    today: date,
    actual_weekly_hours: float | None = None,
    garmin_metrics: list[dict] | None = None,
    client: Any | None = None,
) -> AthleteState:
    """Översätt athlete_profiles + weekly_report + TP-cache → AthleteState."""
    phase_state = athlete.get("phase_state") or {}

    # Self-rapporterad återhämtning från senaste veckorapport
    feels_rested = False
    if weekly_report:
        sleep = weekly_report.get("sleep_quality") or 0
        energy = weekly_report.get("energy") or 0
        feels_rested = sleep >= 4 and energy >= 4

    # Faktisk veckotid vinner över deklarerad
    declared = float(athlete.get("weekly_hours") or 0)
    if actual_weekly_hours is not None and actual_weekly_hours > 0:
        weekly_hours = actual_weekly_hours
    else:
        weekly_hours = declared
    # Golv: planera ALLTID minst 5 h/v, oavsett vad historiken visar. Att fördela
    # 1,7 h över en vecka blir meningslöst — minst en base-nivå (5 h) prescriberas.
    # UNDANTAG: inom transition-fönstret efter ett genomfört lopp är låg volym
    # poängen — där får planen gå under golvet.
    last_race_days, last_race_dist = _last_race_info(athlete, today, client)
    in_transition_window = (
        last_race_days is not None
        and last_race_days <= transition_days_for(last_race_dist)
    )
    if not in_transition_window:
        weekly_hours = max(weekly_hours, MIN_PLANNED_WEEKLY_HOURS)

    # OT-tecken från TP-matad kompatibilitetscache (snabb-koll innan full assessment)
    has_ot = False
    if garmin_metrics:
        latest = garmin_metrics[0]
        readiness = latest.get("readiness_score")
        if readiness is not None and readiness < 60:
            has_ot = True
        # HRV under baseline_low?
        hrv = latest.get("hrv_last_night_ms")
        baseline_low = latest.get("hrv_baseline_low")
        if hrv and baseline_low and hrv < float(baseline_low) * 0.9:
            has_ot = True

    return AthleteState(
        weekly_training_hours=weekly_hours,
        has_injury=_has_significant_injury(athlete),
        has_overtraining_signs=has_ot,
        weeks_until_next_race=_weeks_until_race(athlete, today, client),
        last_race_completed_within_days=last_race_days,
        last_race_distance=last_race_dist,
        current_phase=phase_state.get("current_phase"),
        weeks_in_current_phase=phase_state.get("weeks_in_phase"),
        athlete_feels_rested=feels_rested,
        has_high_specific_fitness=False,  # subjektiv, coach sätter via override
    )


def _build_ot_signals(
    athlete: dict,
    weekly_report: dict | None,
    garmin_metrics: list[dict] | None = None,
) -> OvertrainingSignals:
    """Bygg OT-signaler från TP-cache + adept-rapporter.

    TP fyller garmin_coach.daily_metrics som kompatibilitetscache för objektiva
    signaler (RHR, HRV, sömn, ACWR).
    Weekly report ger subjektiva signaler (motivation, energi).
    active_concerns ger injury_present-flaggan.
    """
    # Objektiva signaler från TP-matad kompatibilitetscache.
    rhr_delta = None
    hrv_pct = None
    sleep_avg = None
    sleep_low_streak = None
    high_load_streak = None
    readiness = None
    if garmin_metrics:
        rhr_delta = _compute_rhr_delta(garmin_metrics)
        hrv_pct = _compute_hrv_pct_below(garmin_metrics)
        sleep_avg = _compute_sleep_avg(garmin_metrics, days=7)
        sleep_low_streak = _compute_sleep_low_streak(garmin_metrics)
        high_load_streak = _compute_consecutive_high_load_weeks(garmin_metrics)
        # Senaste readiness om leverantören fyller den. TP lämnar den normalt tom.
        readiness = garmin_metrics[0].get("readiness_score")
        if readiness is not None:
            readiness = float(readiness)

    # Subjektiva från weekly_report
    motivation_low = False
    poor_recovery = False
    persistent_fatigue = False
    if weekly_report:
        motivation_low = (weekly_report.get("motivation") or 5) <= 2
        sleep_q = weekly_report.get("sleep_quality") or 5
        soreness = weekly_report.get("soreness") or 5
        poor_recovery = sleep_q <= 2 or soreness <= 2
        energy = weekly_report.get("energy") or 5
        persistent_fatigue = energy <= 2

    return OvertrainingSignals(
        rhr_bpm_over_baseline=rhr_delta,
        hrv_pct_below_baseline=hrv_pct,
        sleep_score_avg_7d=sleep_avg,
        sleep_consecutive_low_days=sleep_low_streak,
        readiness_score=readiness,
        consecutive_high_load_weeks=high_load_streak,
        motivation_low=motivation_low,
        poor_recovery=poor_recovery,
        muscle_fatigue_persistent=persistent_fatigue,
        injury_present=_has_significant_injury(athlete),
    )


# Konsoliderad till coach.engine.profile.profile_from_athlete_row (2026-07-02).
# Aliaset behålls — cron.py och tester importerar namnet härifrån.
_build_athlete_profile_for_zones = profile_from_athlete_row


# ---------- Engine-orkestrering ----------


def _resolve_period_position(
    athlete: dict,
    phase: str,
    week_start: date,
    override_week: int | None = None,
    override_len: int | None = None,
) -> tuple[int, int, int]:
    """Bestäm (week_in_period, weeks_in_period, weeks_in_phase) för veckan.

    Cykellängden styrs av adeptens recovery_week_ratio ('3:1' → 4-veckors-
    cykel, '2:1' → 3-veckors för masters). Positionen räknas från
    phase_state.weeks_in_phase som planner skriver tillbaka vid apply.
    Sista veckan i cykeln är återhämtnings-/testvecka (constraints i
    phase_details droppar hårda kategorier + volymen skalas).

    Explicita CLI/API-argument (override_week/override_len) vinner alltid.
    Re-körning av samma vecka (last_planned_week_start) ökar inte räknaren.
    """
    ratio = str(athlete.get("recovery_week_ratio") or "3:1")
    try:
        cycle_len = int(ratio.split(":")[0]) + 1
    except (ValueError, IndexError):
        cycle_len = 4

    ps = athlete.get("phase_state") or {}
    same_phase = ps.get("current_phase") == phase
    prev_weeks = ps.get("weeks_in_phase") if same_phase else 0
    if not isinstance(prev_weeks, int) or prev_weeks < 0:
        prev_weeks = 0
    if same_phase and ps.get("last_planned_week_start") == week_start.isoformat():
        weeks_in_phase = max(1, prev_weeks)  # re-körning av samma vecka
    else:
        weeks_in_phase = prev_weeks + 1

    weeks_in_period = override_len or cycle_len
    if override_week is not None:
        week_in_period = override_week
    else:
        week_in_period = ((weeks_in_phase - 1) % weeks_in_period) + 1
    return week_in_period, weeks_in_period, weeks_in_phase


def _apply_week_volume_scaling(
    decisions: dict,
    phase: str,
    week_in_period: int,
    weeks_in_period: int,
    weeks_in_phase: int,
) -> dict:
    """Skala veckans volym för taper (peak) och återhämtningsvecka.

    Peak: factor_per_week ** veckor-i-fasen (phase_details.volume_reduction_rule).
    Prep/base/build: sista veckan i cykeln skalas med recovery_week.volume_factor.
    Skalan appliceras på discipline_hours (som styr passlängderna) och loggas
    spårbart i decisions.
    """
    details = load_yaml("phase_details.yaml")
    factor: float | None = None

    recovery = {"active": False, "week_in_period": week_in_period,
                "weeks_in_period": weeks_in_period}

    if phase == "peak":
        rule = (details["phase_details"].get("peak") or {}).get(
            "volume_reduction_rule"
        ) or {}
        per_week = float(rule.get("factor_per_week", 0.75))
        factor = per_week ** max(1, weeks_in_phase)
        decisions["taper"] = {
            "factor_per_week": per_week,
            "week_in_phase": weeks_in_phase,
            "factor": round(factor, 3),
        }
    elif phase == "transition":
        # Efter genomfört lopp: lätt, valfri träning — får gå under 5h-golvet.
        factor = float(
            (details["phase_details"].get("transition") or {}).get(
                "volume_factor", 0.25
            )
        )
        decisions["transition"] = {"volume_factor": factor}
    else:
        rec_rule = details.get("recovery_week") or {}
        if (
            phase in (rec_rule.get("applies_to_phases") or [])
            and week_in_period == weeks_in_period
        ):
            factor = float(rec_rule.get("volume_factor", 0.6))
            recovery.update(active=True, volume_factor=factor)

    decisions["recovery_week"] = recovery
    if factor is not None:
        dh = decisions.get("discipline_hours") or {}
        decisions["discipline_hours"] = {
            d: round(h * factor, 2) for d, h in dh.items()
        }
    return decisions


def _build_nutrition(
    athlete: dict,
    phase: str,
    week_in_period: int,
    weeks_in_period: int,
) -> dict | None:
    """Bygg nutrition-beslut: generella defaults + adeptens överridor.

    Returneras för race-fasen och sista peak-veckan (inför tävling), annars None.
    Defaults i data/nutrition.yaml; per-adept-fält i athlete_profiles.
    """
    if not (phase == "race" or (phase == "peak" and week_in_period == weeks_in_period)):
        return None
    try:
        config = load_yaml("nutrition.yaml")
    except FileNotFoundError:
        return None
    defaults = config.get("defaults") or {}
    result = {
        "race_carbs_per_hour_g": athlete.get("race_carbs_per_hour_g")
        or defaults.get("race_carbs_per_hour_g"),
        "carb_load_g_per_kg_per_day": (
            float(athlete["carb_load_g_per_kg"])
            if athlete.get("carb_load_g_per_kg") is not None
            else defaults.get("carb_load_g_per_kg_per_day")
        ),
        "carb_load_days_before": defaults.get("carb_load_days_before"),
        "pre_start_minutes": defaults.get("pre_start_minutes"),
        "pre_start_intake": defaults.get("pre_start_intake"),
        "notes": (athlete.get("nutrition_notes") or "").strip() or None,
        "individualized": bool(
            athlete.get("race_carbs_per_hour_g") or athlete.get("carb_load_g_per_kg")
        ),
    }
    return result


def _run_engine(
    state: AthleteState,
    ot_signals: OvertrainingSignals,
    week_in_period: int,
    weeks_in_period: int,
    phase_rec=None,
    weeks_in_phase: int | None = None,
) -> dict:
    """Kör alla engine-funktioner och samla beslut i en spårbar dict.

    phase_rec kan skickas in förberäknad (generate_week behöver fasen för att
    resolva veckoposition INNAN engine körs) — annars beräknas den här.

    week_in_period/weeks_in_period är VILOVECKOCYKELNS position (3 eller 4
    veckor) och styr kategorival och volymskalning. weeks_in_phase är hur
    långt in i fasen veckan ligger; ur den räknas PERIODENS position för
    styrkeprotokollet. Förut fick styrkan cykelpositionen och halverade
    den — MT→MS→MS→MT var tredje vecka i stället för en gång per base_1.
    """
    phase_rec = phase_rec or determine_phase(state)
    strength_week, strength_len = week_in_period, weeks_in_period
    if weeks_in_phase is not None:
        pos = period_position(phase_rec.phase, phase_rec.period, weeks_in_phase)
        if pos:
            strength_week, strength_len = pos
    category_decisions = workout_type_decisions(
        phase=phase_rec.phase,
        period=phase_rec.period,
        week_in_period=week_in_period,
        weeks_in_period=weeks_in_period,
    )
    categories = [d["code"] for d in category_decisions if d["allowed"]]
    discipline_hours = distribute_weekly_hours(
        phase_rec.phase, state.weekly_training_hours
    )
    hard_cap = hard_training_cap_minutes(
        phase_rec.phase, state.weekly_training_hours * 60
    )
    ot = assess_overtraining(ot_signals)
    adjustment = recommend_adjustment(ot)

    # Strength: undvik fall för faser utan protokoll
    strength_detail = None
    try:
        strength = current_strength_protocol(
            phase=phase_rec.phase,
            period=phase_rec.period,
            week_in_period=strength_week,
            weeks_in_period=strength_len,
        )
        strength_code = strength.protocol_code
        strength_detail = {
            "code": strength.protocol_code,
            "name": strength.protocol_name,
            "intensity": strength.intensity,
            "reps": list(strength.reps),
            "sets": list(strength.sets),
            "reason": strength.reason,
            "sessions_per_week": (
                list(strength.sessions_per_week)
                if strength.sessions_per_week
                else None
            ),
        }
    except ValueError:
        strength_code = "none"

    return {
        "phase_recommendation": {
            "phase": phase_rec.phase,
            "period": phase_rec.period,
            "reason": phase_rec.reason,
            "unmet_criteria": list(phase_rec.unmet_criteria),
            "optimal_phase": phase_rec.optimal_phase,
            "behind": phase_rec.behind,
        },
        "categories": categories,
        "category_decisions": category_decisions,   # kod + tillåten + skäl
        "discipline_hours": discipline_hours,
        "hard_training_cap": hard_cap,
        "overtraining": {
            "level": ot.level,
            "label": ot.label,
            "flag_count": ot.flag_count,
            "flags": list(ot.flags),
        },
        "plan_adjustment": _adjustment_dict(adjustment),
        "strength_protocol": strength_code,
        "strength_protocol_detail": strength_detail,
    }


# ---------- Pass-val ----------


def _phase_filter_value(phase: str, period: str | None) -> str:
    """Konvertera engine-fas/period till värdet som passbankens phase_appropriate
    förväntar (base + base_2 → 'base_2'; prep utan period → 'prep')."""
    value = period or phase
    return "recovery" if value == "transition" else value


def _pass_requires_trainer(w: dict) -> bool:
    """Detektera om passet kräver trainer / fast indoor-cykel."""
    if w.get("requires_trainer"):
        return True
    equipment = w.get("equipment") or []
    if "trainer" in equipment:
        return True
    if w.get("setting") == "indoor" and w.get("discipline") == "bike":
        return True
    return False


def _pass_setting(w: dict) -> str:
    """Returnera 'indoor', 'outdoor' eller 'either' baserat på pass-flaggor."""
    if w.get("outdoor_only"):
        return "outdoor"
    if w.get("requires_trainer"):
        return "indoor"
    setting = w.get("setting")
    if setting in ("indoor", "outdoor"):
        return setting
    if setting == "either":
        return "either"
    # Default
    return "either"


def _passes_equipment_filter(w: dict, equipment: dict) -> bool:
    """True om adept har den utrustning passet kräver."""
    disc = w.get("discipline")

    # Pool-typ
    if disc == "swim":
        pool_type = equipment.get("pool_type", "25m")
        if pool_type == "none":
            return False
        # Öppet vatten-pass kräver tillgång till öppet vatten
        equip = w.get("equipment") or []
        if "open_water" in equip and pool_type != "open_water":
            return False

    # Trainer
    if disc == "bike" and _pass_requires_trainer(w):
        if not equipment.get("has_trainer", True):
            return False

    # Löpband
    if disc == "run" and w.get("setting") == "indoor":
        if not equipment.get("has_treadmill", False):
            return False

    return True


def _select_workout_for(
    category: str,
    discipline: str,
    phase_filter: str,
    workouts_pool: list[dict],
    recent_codes: set[str],
    rng: random.Random,
    equipment: dict | None = None,
    preferred_settings: dict | None = None,
) -> dict | None:
    """Välj ett pass för given kategori + disciplin.

    Filtreringsordning:
      1. Match category + discipline + phase_filter
      2. Utrustnings-filter (hård — saknar utrustning = skippa)
      3. Variation-filter (undvik pass kört de senaste 4 veckorna)
      4. Setting-val (hårt när adepten valt indoor/outdoor)
    """
    candidates = [
        w
        for w in workouts_pool
        if w.get("category") == category
        and w.get("discipline") == discipline
        and phase_filter in (w.get("phase_appropriate") or [])
    ]
    if not candidates:
        return None

    # I AE-fallet: föredra AE2-pass (endurance) över AE1 (recovery) —
    # adept får ett "richtigt" långpass på AE-veckorna istället för en
    # kort recovery-tur när det egentligen ska byggas volym.
    if category == "AE":
        ae2 = [w for w in candidates if (w.get("type_code") or "").startswith("AE2")]
        if ae2:
            candidates = ae2

    # 2. Utrustnings-filter (hård)
    if equipment is not None:
        equip_filtered = [w for w in candidates if _passes_equipment_filter(w, equipment)]
        if equip_filtered:
            candidates = equip_filtered
        # Om alla föll bort: returnera None (saknar utrustning för denna kategori)
        if not candidates:
            return None

    # 3. Variation (mjuk)
    fresh = [w for w in candidates if w.get("code") not in recent_codes]
    candidates = fresh or candidates

    # 4. Setting-val. "any" är fritt; indoor/outdoor är ett krav.
    if preferred_settings is not None:
        pref = preferred_settings.get(discipline, "any")
        if pref != "any":
            candidates = [
                w for w in candidates
                if _pass_setting(w) in (pref, "either")
            ]
            if not candidates:
                return None

    return rng.choice(candidates)


def _pick_long_workout_duration(
    workout: dict,
    discipline_hours: float,
    is_long_day: bool,
    max_minutes: int | None = None,
) -> int:
    """Bestäm duration för parameterized pass.

    Heuristik:
      - Långpass-dag → max-spannet inom rimliga gränser (~50% av disciplinens veckotid)
      - Annars → default-värdet
      - max_minutes = fasens/periodens max passlängd (phase_details) — hårt tak
    """
    if not workout.get("parameterized"):
        td = workout.get("total_duration_min") or {}
        return int(td.get("estimated") or 60)

    params = workout.get("parameters") or {}
    dur_param = params.get("duration_min") or {}

    if isinstance(dur_param, dict):
        default = dur_param.get("default") or 60
        if is_long_day:
            # Använd 60-80% av spannets max, kapat mot disciplinens veckotid
            max_dur = (
                dur_param.get("max")
                or (dur_param.get("range") or [default, default])[-1]
            )
            target = int(max_dur * 0.75)
            cap = int(discipline_hours * 60 * 0.5)  # max 50% av veckans disc-tid
            result = min(target, cap)
        else:
            result = int(default)
        if max_minutes is not None:
            result = min(result, max_minutes)
        return result

    return 60


# ---------- Schemaläggning ----------


_DAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _renormalize_hours(
    discipline_hours: dict[str, float], active_sports: list[str]
) -> dict[str, float]:
    """Behåll bara aktiva discipliner och fördela bortagna timmar proportionellt.

    Ex: weekly_hours=12, default-split run=4.2/bike=5.4/swim=2.4.
    Om bara run+swim är aktiva: skala så summan fortfarande är 12h, med
    samma run:swim-ratio som ursprungligen (4.2 : 2.4 ≈ 64% : 36%).
    """
    total_original = sum(discipline_hours.values())
    if total_original <= 0:
        return {d: 0.0 for d in active_sports}

    filtered = {d: h for d, h in discipline_hours.items() if d in active_sports}
    if not filtered:
        return {d: 0.0 for d in active_sports}

    new_total = sum(filtered.values())
    if new_total <= 0:
        return {d: 0.0 for d in active_sports}

    scale = total_original / new_total
    return {d: round(h * scale, 2) for d, h in filtered.items()}


def _estimated_duration_minutes(workout: dict) -> int:
    """Snabb upptäckt av defaultlängd, även för parameterized templates.

    För parameterized: läs parameters.duration_min.default.
    För resolvade/konkreta pass: läs total_duration_min.estimated.
    """
    if workout.get("parameterized"):
        params = workout.get("parameters") or {}
        d = params.get("duration_min")
        if isinstance(d, dict):
            return int(d.get("default") or 60)
        if isinstance(d, (int, float)):
            return int(d)
        return 60
    td = workout.get("total_duration_min") or {}
    est = td.get("estimated")
    if isinstance(est, (int, float)):
        return int(est)
    if isinstance(est, str):
        # Templated string vi inte rensat — fall tillbaka till 60
        return 60
    return 60


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _neighbor_disciplines(
    schedule: dict[str, ScheduledWorkout], day: str
) -> set[str]:
    """Disciplinerna på dagen före och dagen efter."""
    idx = _DAY_INDEX[day]
    out: set[str] = set()
    for delta in (-1, 1):
        n_idx = idx + delta
        if 0 <= n_idx < 7:
            n_day = _DAYS[n_idx]
            if n_day in schedule:
                out.add(schedule[n_day].sport)
    return out


def _can_place(
    schedule: dict[str, ScheduledWorkout], day: str, discipline: str
) -> bool:
    """True om disciplinen inte krockar med någon granne."""
    return discipline not in _neighbor_disciplines(schedule, day)


def _schedule_workouts(
    selected: list[dict],
    discipline_hours: dict[str, float],
    week_start: date,
    long_bike_day: str | None,
    long_run_day: str | None,
    rest_days: list[str],
    strength_workout: dict | None,
    locked: list[ScheduledWorkout] | None = None,
    reserved_dates: set[date] | None = None,
    existing_workout_count: int = 0,
    equipment: dict | None = None,
    preferred_settings: dict | None = None,
    strength_sessions: int = 1,
    phase: str | None = None,
    period: str | None = None,
) -> list[ScheduledWorkout]:
    """Fördela utvalda pass över veckodagar.

    Schemaprinciper (i ordning):
      1. Vilodagar låses från `preferred_rest_days` (eller default måndag).
      2. Långpass-bike (största AE bike) på `long_bike_day` (default lördag).
      3. Långpass-löp (största AE run) på `long_run_day` (default söndag).
      4. Kvalitetspass (ME/AC/MF/TE) — använd båda-grannar-constraint:
         placera inte samma disciplin på dag före ELLER dag efter.
      5. Speed (SS) — samma constraint.
      6. Volym (AE som inte är långpass) — samma constraint.
      7. Strength — konkret pass ur passbanken, upp till `strength_sessions`
         pass (protokollets sessions_per_week) på lediga kvalitetsdagar.
      8. Fyll till minst fem pass inklusive mänskligt planerade pass.
      9. Skriv uttrycklig vila på återstående dagar.

    phase/period används för fasens max passlängd (phase_details) som tak
    på parameterized pass-längder.

    Brick: BW-kategori = bike+run kombo. När det finns konkreta brick-pass i
    passbanken (filer brick_*.yaml) placeras de på long_bike_day eftersom
    brick ÄR bike följt av run i samma pass — inte separata dagar.
    """
    day_dates = {day: week_start + timedelta(days=_DAY_INDEX[day]) for day in _DAYS}
    schedule: dict[str, ScheduledWorkout] = {}

    def _max_minutes_for(w: dict) -> int | None:
        """Fasens/periodens max passlängd för passets disciplin (om definierad)."""
        if phase is None:
            return None
        try:
            return max_session_minutes(phase, w.get("discipline"), period)
        except (ValueError, KeyError):
            return None

    def _sfw(w: dict, dt: date, is_long: bool = False) -> ScheduledWorkout:
        """Skapa pass med rätt längd-skalning mot disciplinens faktiska veckotid."""
        return _scheduled_from_workout(
            w, dt, is_long=is_long,
            discipline_hours=discipline_hours.get(w.get("discipline"), 6.0),
            max_minutes=_max_minutes_for(w),
        )

    # Dagar med Nils-/manualpass reserveras. Platshållarna skrivs aldrig till DB.
    for reserved in reserved_dates or set():
        if week_start <= reserved <= week_start + timedelta(days=6):
            day_name = _DAYS[reserved.weekday()]
            schedule[day_name] = ScheduledWorkout(
                date=reserved,
                sport="rest",
                code="reserved_human",
                title="Reserverad för mänskligt pass",
                category="RESERVED",
                duration_minutes=0,
                intensity="—",
            )

    # 0. Låsta pass — placera först, dessa rörs aldrig av algoritmen
    for lock in locked or []:
        day_name = _DAYS[lock.date.weekday()]
        schedule[day_name] = lock

    # 1. Vilodagar
    for d in rest_days or []:
        if d in _DAYS and d not in schedule:
            schedule[d] = ScheduledWorkout(
                date=day_dates[d],
                sport="rest",
                code="rest",
                title="Vilodag",
                category="REST",
                duration_minutes=0,
                intensity="—",
                notes="Aktiv vila eller rörlighet 15-30 min är OK.",
            )

    # Sortera passen i kategorier
    def kind_of(w: dict) -> str:
        cat = w.get("category", "")
        if cat in ("ME", "AC", "MF", "TE"):
            return "quality"
        if cat == "AE":
            return "volume"
        if cat == "SS":
            return "speed"
        if cat == "BW":
            return "brick"  # bike+run kombo — placeras med bike-långpasset
        if cat == "T":
            return "test"
        return "other"

    quality = [w for w in selected if kind_of(w) == "quality"]
    volume = [w for w in selected if kind_of(w) == "volume"]
    speed = [w for w in selected if kind_of(w) == "speed"]
    brick_pool = [w for w in selected if kind_of(w) == "brick"]
    other = [w for w in selected if kind_of(w) in ("test", "other")]

    # 2-3. Långpass per disciplin
    def place_long(
        target_day: str | None,
        discipline: str,
        default_day: str,
    ) -> None:
        """Placera längsta AE-pass i given disciplin på preferred dag."""
        if target_day is None:
            # "spelar ingen roll" — välj default men kontrollera grannar
            candidates = [default_day] + [d for d in _DAYS if d != default_day]
        else:
            candidates = [target_day] + [d for d in _DAYS if d != target_day]

        # Om brick-pass finns och disciplin är bike — brick VINNER långdagen
        # (brick ÄR veckans stora cykeldag + löpning på), annars största AE.
        if discipline == "bike" and brick_pool:
            pool = brick_pool
        else:
            pool = volume
        disc_pool = [w for w in pool if w.get("discipline") in (discipline, "brick")]
        if not disc_pool:
            return

        chosen = max(disc_pool, key=_estimated_duration_minutes)

        for d in candidates:
            if d in schedule:
                continue
            # Tillåt långpass även om granne är samma disciplin —
            # långpasset är kärnan, det måste placeras
            schedule[d] = _sfw(chosen, day_dates[d], is_long=True)
            if chosen in volume:
                volume.remove(chosen)
            elif chosen in brick_pool:
                brick_pool.remove(chosen)
            return

    place_long(long_bike_day, "bike", default_day="saturday")
    place_long(long_run_day, "run", default_day="sunday")

    # 4-6. Kvalitet + speed + volym med båda-grannar-constraint
    def place_with_neighbor_constraint(pass_list: list[dict]) -> None:
        for w in list(pass_list):
            disc = w.get("discipline", "?")
            # Försök hitta en dag där grannarna INTE redan har samma disciplin
            placed = False
            for d in _DAYS:
                if d in schedule:
                    continue
                if _can_place(schedule, d, disc):
                    schedule[d] = _sfw(w, day_dates[d])
                    pass_list.remove(w)
                    placed = True
                    break
            if not placed:
                # Inget perfekt val — ta första lediga dag
                for d in _DAYS:
                    if d not in schedule:
                        schedule[d] = _sfw(w, day_dates[d])
                        pass_list.remove(w)
                        break

    # Ordning: kvalitet (hårdast → prio), sen speed (lätta tillägg), sen volym, sen övrigt
    place_with_neighbor_constraint(quality)
    place_with_neighbor_constraint(speed)
    place_with_neighbor_constraint(volume)
    place_with_neighbor_constraint(other)
    place_with_neighbor_constraint(brick_pool)  # om kvar (oplacerade brick)

    # 6b. Konkret styrkepass ur passbanken. Styrka räknas mot veckans fem pass.
    # Antal pass styrs av protokollets sessions_per_week (AA/MT/MS: 2, SM: 1).
    if strength_workout:
        placed_strength = 0
        for d in ("wednesday", "friday", "tuesday", "thursday"):
            if placed_strength >= max(1, strength_sessions):
                break
            if d not in schedule:
                schedule[d] = _sfw(strength_workout, day_dates[d])
                placed_strength += 1

    # 6c. Hård regel: minst 5 pass per vecka (vila räknas inte).
    # Om engine + partial-impact gett oss <5 pass, fyll tomma dagar med
    # extra AE-volym i discipliner som inte har full impact. Frekvens är
    # viktigare än tid — passen kan vara korta.
    _ensure_minimum_workouts(
        schedule=schedule,
        day_dates=day_dates,
        discipline_hours=discipline_hours,
        min_total_workouts=max(0, 5 - existing_workout_count),
        equipment=equipment,
        preferred_settings=preferred_settings,
    )

    # 7. Alla dagar ska synas. Tom dag betyder uttrycklig planerad vila.
    for d in _DAYS:
        if d not in schedule:
            schedule[d] = ScheduledWorkout(
                date=day_dates[d],
                sport="rest",
                code="rest",
                title="Vilodag",
                category="REST",
                duration_minutes=0,
                intensity="—",
                notes="Planerad vila. Lätt rörlighet eller promenad är valfritt.",
            )

    return [
        schedule[d] for d in _DAYS
        if schedule[d].code != "reserved_human"
    ]


def _count_discipline_balance(
    schedule: dict[str, ScheduledWorkout],
) -> dict[str, int]:
    """Räkna pass per disciplin i nuvarande schema (ignorerar rest/strength)."""
    counts: dict[str, int] = {"swim": 0, "bike": 0, "run": 0}
    for w in schedule.values():
        if w.sport in counts:
            counts[w.sport] += 1
    return counts


def _ensure_minimum_workouts(
    schedule: dict[str, ScheduledWorkout],
    day_dates: dict[str, date],
    discipline_hours: dict[str, float],
    min_total_workouts: int = 5,
    equipment: dict | None = None,
    preferred_settings: dict | None = None,
) -> None:
    """Hård regel: minst N pass per vecka, fyll lediga dagar.

    Strategi:
      1. Om <N pass: fyll alla lediga dagar med extra AE-volym.
      2. Vid varje placement: prioritera disciplin som har MINST pass
         hittills (balansering mot discipline_hours-ratio).
      3. Respektera båda-grannar-constraint när möjligt, fall tillbaka
         till "valfri ledig dag" som sista utväg.

    Modifierar `schedule` in-place.
    """
    def _sfw(w: dict, dt: date, is_long: bool = False) -> ScheduledWorkout:
        return _scheduled_from_workout(
            w, dt, is_long=is_long,
            discipline_hours=discipline_hours.get(w.get("discipline"), 6.0),
        )

    workout_count = sum(1 for w in schedule.values() if w.sport != "rest")
    free_days = [d for d in _DAYS if d not in schedule]

    # Inga lediga dagar → kan inte fylla mer
    if not free_days:
        return
    # Inte under min OCH inga lediga dagar att fylla med kvalitet
    if workout_count >= min_total_workouts and not free_days:
        return

    pool = load_workouts()
    # Bara discipliner som har positiv tid (inte blockerade av impact=full)
    active_disciplines = [d for d, h in discipline_hours.items() if h > 0]
    if not active_disciplines:
        return

    rng = random.Random(0)

    def _eligible(w: dict, disc: str) -> bool:
        if equipment is not None and not _passes_equipment_filter(w, equipment):
            return False
        pref = (preferred_settings or {}).get(disc, "any")
        return pref == "any" or _pass_setting(w) in (pref, "either")

    for d in free_days:
        workout_count = sum(1 for w in schedule.values() if w.sport != "rest")
        if workout_count >= min_total_workouts:
            break
        # Räkna nuvarande balans — välj disciplin med MINST pass hittills.
        # Vid lika, föredra den med mest timmar-budget.
        counts = _count_discipline_balance(schedule)
        active_counts = [(disc, counts.get(disc, 0)) for disc in active_disciplines]
        # Sortera: (antal pass asc, timmar desc)
        active_counts.sort(
            key=lambda x: (x[1], -discipline_hours.get(x[0], 0))
        )
        preferred_order = [disc for disc, _ in active_counts]

        placed = False
        for disc in preferred_order:
            if not _can_place(schedule, d, disc):
                continue
            candidates = [
                w for w in pool
                if w.get("category") == "AE"
                and w.get("discipline") == disc
                and _eligible(w, disc)
            ]
            if candidates:
                chosen = rng.choice(candidates)
                schedule[d] = _sfw(chosen, day_dates[d], is_long=False)
                placed = True
                break

        # Om ingen disciplin passade granne-constraint, ta första aktiva
        if not placed:
            for disc in preferred_order:
                candidates = [
                    w for w in pool
                    if w.get("category") == "AE"
                    and w.get("discipline") == disc
                    and _eligible(w, disc)
                ]
                if candidates:
                    chosen = rng.choice(candidates)
                    schedule[d] = _sfw(chosen, day_dates[d], is_long=False)
                    break


def _scheduled_from_workout(
    workout: dict,
    dt: date,
    is_long: bool = False,
    discipline_hours: float = 6.0,
    max_minutes: int | None = None,
) -> ScheduledWorkout:
    """Konvertera passbankens workout-dict → ScheduledWorkout.

    discipline_hours = adeptens veckotid för passets disciplin; styr hur långt ett
    parameterized långpass blir (cap = 50 % av disciplinens veckotid). Var tidigare
    hårdkodat 6.0 → alla fick 6h-veckans längder (t.ex. 3h-cykel) oavsett volym.
    max_minutes = fasens/periodens max passlängd från phase_details (extra tak).
    """
    resolved = (
        resolve_template(
            workout,
            {"duration_min": _pick_long_workout_duration(
                workout, discipline_hours, is_long, max_minutes
            )},
        )
        if workout.get("parameterized")
        else workout
    )
    td = resolved.get("total_duration_min") or {}
    duration = int(td.get("estimated") or 60)

    zones = resolved.get("zone_refs") or []
    if resolved.get("discipline") == "strength":
        intensity = f"{resolved.get('type_code', 'styrka')} · RIR-styrt"
    else:
        intensity = ", ".join(str(z) for z in zones) if zones else "Z2"

    return ScheduledWorkout(
        date=dt,
        sport=resolved.get("discipline", "swim"),
        code=resolved.get("code", "?"),
        title=resolved.get("name", "?"),
        category=resolved.get("category", "?"),
        duration_minutes=duration,
        intensity=intensity,
        workout_data=resolved,
        notes=(resolved.get("intent") or "").strip(),
    )


# ---------- Skriv till MASTER planned_sessions (docs/08, steg 4) ----------
# (De gamla persist-funktionerna mot training_plans/training_weeks/workouts
#  togs bort 2026-07-02 — tabellerna droppades i datamodell-konsolideringen.)

# Trixa2-disciplin → planned_sessions.sport (korrekt svenska, matchar Nils/dashboard).
_PS_SPORT = {k: sports.sv(k) for k in sports.PLANNABLE_KEYS}


def _planned_session_row(
    sw: ScheduledWorkout, user_id: str, exercise_map: dict[str, dict] | None = None
) -> dict:
    """ScheduledWorkout → planned_sessions-rad (origin='trixa2')."""
    workout = sw.workout_data or {}
    steps = workout.get("main_set", [])
    # Mallens repspann (parameters.reps.range) följer med varje övning: utan
    # golv och tak vet progressionen inte när reps ska växlas mot tyngre vikt.
    reps_range = (workout.get("parameters") or {}).get("reps")
    return {
        "user_id": user_id,
        "date": sw.date.isoformat(),
        "sport": _PS_SPORT.get(sw.sport, sw.sport),
        "title": sw.title,
        "details": (sw.details_markdown or sw.notes or "").strip(),
        "purpose": sw.category,
        "status": "planned",
        "duration_min": sw.duration_minutes,
        "steps": steps,
        # Övningarna som strukturerad lista, inte bara som prosa i details.
        # Utan den kan loggformuläret inte förifyllas och adepten får skriva
        # av tolv övningsnamn som Trixa själv genererat.
        "exercises": exercises_from_steps(steps, exercise_map, reps_range) or None,
        "workout_code": sw.code,
        "intensity": sw.intensity,
        "origin": "trixa2",
    }


def _persist_to_planned_sessions(client, plan: WeekPlan, user_id: str) -> dict:
    """Skriv veckans pass till MASTER public.planned_sessions.

    Befintliga Trixa-rader uppdateras per datum så deras id och TP-koppling
    överlever regenerering. Rader som inte längre ingår markeras `cancelled`;
    TP-workern tar då bort motsvarande TP-pass innan länken rensas.
    Mänskliga rader (Nils/manual/legacy) rörs aldrig.
    """
    if not user_id:
        return {"written": 0, "updated": 0, "inserted": 0, "cancelled": 0}
    week_start = plan.week_start
    week_end = (week_start + timedelta(days=6)).isoformat()
    existing = (
        client.table("planned_sessions")
        .select("id,date,status")
        .eq("user_id", user_id)
        .eq("origin", origins.ENGINE)
        .gte("date", week_start.isoformat())
        .lte("date", week_end)
        .execute()
    ).data or []

    by_date: dict[str, list[dict]] = {}
    for row in existing:
        by_date.setdefault(str(row.get("date"))[:10], []).append(row)

    used_ids: set[str] = set()
    inserted = 0
    updated = 0
    kept = 0
    today = clock.today()
    exercise_map = {e["code"]: e for e in load_strength_exercises()}
    for sw in plan.workouts:
        payload = _planned_session_row(sw, user_id, exercise_map)
        candidates = by_date.get(payload["date"], [])
        current = next(
            (row for row in candidates if row.get("status") != "cancelled"),
            candidates[0] if candidates else None,
        )
        # Det som redan hänt är historik, inte plan. En regenerering mitt i
        # veckan (t.ex. "byt gren" på lördag) skrev förut om måndagens
        # genomförda pass till 'planned' med ett annat innehåll — och pushade
        # det till klockan igen. Genomförda rader och passerade dagar rörs
        # inte; de behålls som de står.
        if current and (
            current.get("status") == "completed"
            or str(current.get("date"))[:10] < today.isoformat()
        ):
            used_ids.add(str(current["id"]))
            kept += 1
            continue
        if current:
            (
                client.table("planned_sessions")
                .update(payload)
                .eq("id", current["id"])
                .eq("user_id", user_id)
                .eq("origin", origins.ENGINE)
                .execute()
            )
            used_ids.add(str(current["id"]))
            updated += 1
        else:
            client.table("planned_sessions").insert(payload).execute()
            inserted += 1

    cancelled = 0
    for row in existing:
        if str(row.get("id")) in used_ids or row.get("status") == "cancelled":
            continue
        (
            client.table("planned_sessions")
            .update({"status": "cancelled"})
            .eq("id", row["id"])
            .eq("user_id", user_id)
            .eq("origin", origins.ENGINE)
            .execute()
        )
        cancelled += 1

    return {
        "written": inserted + updated,
        "updated": updated,
        "inserted": inserted,
        "kept": kept,
        "cancelled": cancelled,
    }


# ---------- Override-hantering ----------


def _apply_phase_override(
    phase_rec: PhaseRecommendation, overrides: list[dict]
) -> tuple[PhaseRecommendation, list[dict]]:
    """Coachens fas-override, applicerad INNAN veckopositionen räknas.

    Applicerades den efteråt (som förut) räknades vilovecko-cykel,
    kategorier, volymskalning och nutrition för motorns fas medan
    passfiltret och phase_state fick override-fasen. Nästa vecka jämförde
    positionsräknaren lagrad fas (override) med motorns fas → olika →
    weeks_in_phase = 1 om och om igen: ingen vilovecka, ingen taper.
    """
    honored: list[dict] = []
    for ov in overrides:
        decision = ov.get("override_decision") or {}
        if ov.get("scope") == "phase" and decision.get("phase"):
            phase_rec = PhaseRecommendation(
                phase=decision["phase"],
                period=decision.get("period"),
                optimal_phase=phase_rec.optimal_phase,
                behind=phase_rec.behind,
                unmet_criteria=list(phase_rec.unmet_criteria),
                reason=f"Override: {ov.get('motivation', '')}",
            )
            honored.append(ov)
    return phase_rec, honored


def effective_phase_rec(athlete: dict, client, today: date) -> PhaseRecommendation:
    """Fasen som gäller för adepten just nu — samma väg som generate_week.

    Med databas (tävlingen i public.races, faktisk volym) och med coachens
    fas-override. Dashboard och pass-byten körde förut hela motorn utan
    klient och med "vecka 1 av 6" hårdkodat, och kunde landa i en annan fas
    än den planen faktiskt byggts i.
    """
    actual_hours = _fetch_actual_weekly_hours(client, athlete.get("user_id"), today)
    state = _build_athlete_state(
        athlete, None, today, actual_weekly_hours=actual_hours, client=client
    )
    phase_rec = determine_phase(state)
    if athlete.get("id"):
        overrides = _fetch_active_overrides(client, athlete["id"])
        phase_rec, _ = _apply_phase_override(phase_rec, overrides)
    return phase_rec


def _adjustment_dict(adjustment) -> dict | None:
    """PlanAdjustment → spårbar dict i decisions."""
    if not adjustment:
        return None
    return {
        "level": adjustment.level,
        "volume_reduction_pct": adjustment.volume_reduction_pct,
        "intensity_reduction_pct": adjustment.intensity_reduction_pct,
        "extra_rest_days": adjustment.extra_rest_days,
        "swap_to_low_intensity": adjustment.swap_to_low_intensity,
        "consider_medical_consultation": adjustment.consider_medical_consultation,
    }


def _active_volume_factor(decisions: dict) -> float:
    """Den skalning veckan redan bär (vilovecka, taper, transition)."""
    recovery = decisions.get("recovery_week") or {}
    if recovery.get("active"):
        return float(recovery.get("volume_factor", 1.0))
    if decisions.get("transition"):
        return float(decisions["transition"].get("volume_factor", 1.0))
    taper = decisions.get("taper") or {}
    if taper.get("factor"):
        return float(taper["factor"])
    return 1.0


def _apply_overrides(
    engine_decisions: dict,
    overrides: list[dict],
    skip_phase: bool = False,
) -> tuple[dict, list[dict]]:
    """Applicera aktiva coach_overrides på engine-beslut.

    Returnerar (modified_decisions, honored_list).

    En override som kvitteras som hedrad måste också nå planen. Förut byttes
    bara overtraining-*nivån*, medan plan_adjustment (volym-/intensitets-
    reduktion, extra vilodagar) stod kvar från motorns bedömning — hedrad på
    pappret, ignorerad i praktiken. Och en volym-override kördes efter
    vilovecko-skalningen och kastade ×0,6 medan renderingen fortfarande
    påstod "vilovecka".
    """
    modified = dict(engine_decisions)
    honored: list[dict] = []

    for ov in overrides:
        scope = ov.get("scope")
        decision = ov.get("override_decision") or {}
        motivation = ov.get("motivation", "")
        if scope == "phase" and skip_phase:
            continue  # redan applicerad på phase_rec före veckopositionen
        if scope == "phase" and decision.get("phase"):
            modified["phase_recommendation"] = {
                **modified["phase_recommendation"],
                "phase": decision["phase"],
                "period": decision.get("period"),
                "reason": f"Override: {motivation}",
            }
            honored.append(ov)
        elif scope == "volume" and decision.get("weekly_hours") is not None:
            new_hours = float(decision["weekly_hours"])
            phase = modified["phase_recommendation"]["phase"]
            factor = _active_volume_factor(modified)
            modified["discipline_hours"] = {
                d: round(h * factor, 2)
                for d, h in distribute_weekly_hours(phase, new_hours).items()
            }
            modified["volume_override"] = {
                "weekly_hours": new_hours, "scaled_by": factor,
                "reason": f"Override: {motivation}",
            }
            honored.append(ov)
        elif scope == "overtraining" and decision.get("level"):
            current = modified.get("overtraining") or {}
            assessment = OvertrainingAssessment(
                level=decision["level"],
                label=f"Override: {motivation}",
                flag_count=int(current.get("flag_count") or 0),
                flags=list(current.get("flags") or []),
            )
            modified["overtraining"] = {
                **current,
                "level": assessment.level,
                "label": assessment.label,
            }
            modified["plan_adjustment"] = _adjustment_dict(
                recommend_adjustment(assessment)
            )
            honored.append(ov)
        # week/workout-overrides hanteras inte här — de gäller specifika rader
        # och tillämpas av Nils direkt på planned_sessions.

    return modified, honored


# ---------- TrainingPeaks-push (skriv-väg → klockan) ----------


def _push_to_trainingpeaks(
    plan: WeekPlan, profile: AthleteProfile, pg: Any, user_id: str
) -> dict:
    """Pusha veckans planerade pass till TrainingPeaks — **idempotent**.

    TP→Garmin AutoSync levererar dem till klockan (se docs/06). Egenskaper:

    - **Gated** på env ``TRIXA_PUSH_TO_TP``.
    - **Idempotent:** läser den nyss persisterade ``planned_sessions`` och kör
      ``sync_planned_week_to_tp`` (replace-by-id + skip-if-unchanged). Säker att
      köra om/dagligen utan dubbletter — varje rad länkas till sitt TP-pass via
      ``tp_workout_id``.
    - **Best-effort:** plan-persisteringen är redan klar; TP-fel (utgången
      cookie, nätverk) fångas och rapporteras i planen, kraschar inte.
    - Hoppar över vila/styrka utan steps. Brick/styrka som ändå skickas flaggas
      av writern som "når ej klockan".
    """
    if os.environ.get("TRIXA_PUSH_TO_TP", "").lower() not in ("1", "true", "yes"):
        return {"enabled": False}

    from collections import Counter

    from coach.integrations.trainingpeaks.auth_store import supabase_cookie_provider
    from coach.integrations.trainingpeaks.client import TPClient, TPError
    from coach.integrations.trainingpeaks.workout_writer import sync_planned_week_to_tp

    try:
        client = TPClient(cookie_provider=supabase_cookie_provider(user_id))
        results = sync_planned_week_to_tp(
            client, pg, user_id, plan.week_start,
            css_sec_per_100m=profile.css_sec_per_100m,
            threshold_pace_sec_per_km=profile.threshold_pace_sec_per_km,
            dry_run=False,
        )
        client.close()
    except TPError as e:
        return {"enabled": True, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — TP får aldrig fälla plan-persisteringen
        return {"enabled": True, "error": f"oväntat: {e}"}

    actions = Counter(r.action for r in results)
    return {
        "enabled": True,
        "actions": dict(actions),
        "pushed": actions.get("created", 0) + actions.get("replaced", 0),
        "unchanged": actions.get("unchanged", 0),
        "not_reaching_watch": [r.code for r in results if not r.reaches_watch],
        "warnings": [w for r in results for w in r.warnings],
    }


# ---------- Huvudfunktion ----------


def _trace_data_sources(
    actual_weekly_hours: float | None, athlete: dict,
    garmin_metrics: list[dict] | None, ot_signals: OvertrainingSignals,
) -> dict:
    """Vad motorn såg av TP-cache och utförd volym — för spårbarhet i decisions."""
    latest = garmin_metrics[0] if garmin_metrics else {}
    return {
        "actual_weekly_hours_4w_avg": (
            round(actual_weekly_hours, 1) if actual_weekly_hours else None
        ),
        "declared_weekly_hours": float(athlete.get("weekly_hours") or 0),
        "garmin_metrics_days": len(garmin_metrics) if garmin_metrics else 0,
        "latest_metric_date": latest.get("metric_date"),
        "latest_hrv": latest.get("hrv_last_night_ms"),
        "latest_sleep_score": latest.get("sleep_score"),
        "latest_readiness": latest.get("readiness_score"),
        "ot_signals": {
            "rhr_bpm_over_baseline": ot_signals.rhr_bpm_over_baseline,
            "hrv_pct_below_baseline": ot_signals.hrv_pct_below_baseline,
            "sleep_score_avg_7d": ot_signals.sleep_score_avg_7d,
            "sleep_consecutive_low_days": ot_signals.sleep_consecutive_low_days,
            "readiness_score": ot_signals.readiness_score,
            "consecutive_high_load_weeks": ot_signals.consecutive_high_load_weeks,
        },
    }


def _select_week_workouts(
    decisions: dict, athlete: dict, athlete_id: str, week_start: date,
    recent_workouts: list[dict], active_sports: list[str],
    partial_disciplines: set[str], blocked_by_injury: list[str], raw_sports: list[str],
    impacts: dict,
) -> SimpleNamespace:
    """Steg 4 i generate_week: välj konditions- och styrkepass ur passbanken.

    Utbruten ur en 380-raders funktion (docs/12 I12). Samma rader som förut,
    minus ett dött if-block som såg ut som skadehanteringen men inte var det.
    """
    phase = decisions["phase_recommendation"]["phase"]
    period = decisions["phase_recommendation"]["period"]
    categories = decisions["categories"]
    workouts_pool = load_workouts()
    drills = load_drills()
    strength_exercises = load_strength_exercises()
    recent_codes = {w.get("workout_code") for w in recent_workouts if w.get("workout_code")}
    rng = random.Random(_seed_for(athlete_id, week_start))
    phase_filter = _phase_filter_value(phase, period)
    equipment = athlete.get("equipment") or {}
    preferred_settings = athlete.get("preferred_settings") or {}

    selected: list[dict] = []
    warnings: list[str] = []
    for disc in blocked_by_injury:
        warnings.append(
            f"{disc} skippad denna vecka — skada med impact=full"
        )
    for cat in categories:
        # I disciplin med partial impact: skippa hårda kategorier, behåll bara AE
        for disc in active_sports:
            if disc in partial_disciplines and cat in ("ME", "AC", "MF", "TE", "SS"):
                # Hård träning i partial-disciplin hoppas här; AE-reserven
                # nedan ser till att grenen ändå får ett pass.
                continue
            # BW (brick) är sin egen disciplin i passbanken (brick_*.yaml).
            # Väljs en gång per vecka (via bike-iterationen) och kräver att
            # både bike och run är aktiva — det ÄR bike följt av run.
            if cat == "BW":
                if disc != "bike" or "run" not in active_sports:
                    continue
                lookup_disc = "brick"
            else:
                lookup_disc = disc
            chosen = _select_workout_for(
                cat, lookup_disc, phase_filter, workouts_pool, recent_codes, rng,
                equipment=equipment, preferred_settings=preferred_settings,
            )
            if chosen is None:
                warnings.append(
                    f"Inget pass i passbanken matchar {cat} + {disc} + {phase_filter}"
                )
                continue
            selected.append(chosen)

    # För partial-disciplin: säkerställ att det finns minst ett AE-pass
    # (om engine inte sa AE explicit har vi nu inga pass alls för den disc)
    for disc in partial_disciplines:
        if disc in active_sports and not any(
            w.get("discipline") == disc for w in selected
        ):
            ae_pass = _select_workout_for(
                "AE", disc, phase_filter, workouts_pool, recent_codes, rng,
                equipment=equipment, preferred_settings=preferred_settings,
            )
            if ae_pass:
                selected.append(ae_pass)
                warnings.append(
                    f"{disc}: hårda pass utbytta mot AE pga partial skadeimpact"
                )

    strength_workout = None
    if "strength" in raw_sports and impacts.get("strength") != "full":
        protocol = decisions["strength_protocol"]
        protocol_type = "SM" if protocol == "light_maintenance" else protocol
        strength_candidates = [
            w for w in workouts_pool
            if w.get("discipline") == "strength"
            and w.get("type_code") == protocol_type
            and phase_filter in (w.get("phase_appropriate") or [])
        ]
        if strength_candidates:
            fresh = [
                w for w in strength_candidates
                if w.get("code") not in recent_codes
            ]
            strength_workout = rng.choice(fresh or strength_candidates)
        else:
            warnings.append(
                f"Inget styrkepass matchar protokoll {protocol} + {phase_filter}"
            )
    elif "strength" in raw_sports and impacts.get("strength") == "full":
        warnings.append("strength skippad denna vecka — skada med impact=full")
    return SimpleNamespace(
        selected=selected, strength_workout=strength_workout, warnings=warnings,
        drills=drills, strength_exercises=strength_exercises,
        equipment=equipment, preferred_settings=preferred_settings,
    )


def _persist_week(
    client, plan: "WeekPlan", athlete: dict, athlete_id: str, athlete_user_id: str,
    week_start: date, honored: list[dict], phase: str, period: str | None,
    weeks_in_phase: int, zones_profile, today: date,
) -> None:
    """Steg 6 i generate_week: fem skrivningar med ett gemensamt felkontrakt.

    planned_sessions först — landar den inte kastas felet och inget annat
    avanceras. Övriga (overrides, phase_state, alerts, TP-push) är
    best-effort och rapporterar i engine_decisions. Utbruten (docs/12 I12).
    """
    # MASTER-persist: planen skrivs till planned_sessions (docs/08 steg 4-7).
    # De gamla engine-tabellerna (workouts/training_weeks/training_plans)
    # skrivs INTE längre — planned_sessions är enda plan-källan.
    # Landar inte planen ska inget annat avanceras: förut sväljdes felet
    # här, phase_state räknades upp, ett plan_generated-alert skrevs och
    # cron loggade "Klar" — adepten öppnade en tom vecka på måndagen
    # medan coachens inkorg sade att en plan fanns.
    try:
        persist_result = _persist_to_planned_sessions(
            client, plan, athlete_user_id
        )
    except Exception:
        logger.exception(
            "Kunde inte skriva veckan %s för %s till planned_sessions",
            week_start.isoformat(), athlete_user_id,
        )
        raise
    plan.engine_decisions["planned_sessions_written"] = persist_result["written"]
    plan.engine_decisions["planned_sessions_persist"] = persist_result

    # Stäng honoring-loopen för respekterade overrides.
    plan.engine_decisions["overrides_honored_marked"] = _mark_overrides_honored(
        client, honored
    )

    # Skriv tillbaka phase_state så vilovecko-cykeln räknar vidare nästa
    # vecka (re-körning av samma vecka ökar inte räknaren — se
    # _resolve_period_position). Trixa-lagret skriver; engine läser bara.
    new_phase_state = {
        **(athlete.get("phase_state") or {}),
        "current_phase": phase,
        "period": period,
        "weeks_in_phase": weeks_in_phase,
        "last_planned_week_start": week_start.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        client.table("athlete_profiles").update(
            {"phase_state": new_phase_state}
        ).eq("id", athlete_id).execute()
        plan.engine_decisions["phase_state_written"] = new_phase_state
    except Exception as exc:  # noqa: BLE001
        plan.engine_decisions["phase_state_error"] = str(exc)

    # Skriv strukturerade alerts till coach_alerts
    from coach.trixa.alerts import build_alerts, persist_alerts

    alerts = build_alerts(plan, athlete, today)
    if alerts:
        inserted = persist_alerts(
            client,
            alerts,
            athlete_id=athlete_id,
            athlete_user_id=athlete_user_id,
        )
        plan.engine_decisions["alerts_written"] = len(inserted)

    # 6b. Pusha planerade pass till TrainingPeaks (TP→Garmin AutoSync → klockan).
    # Best-effort + gated på TRIXA_PUSH_TO_TP. Plan-persisteringen ovan är klar
    # och får inte påverkas av ev. TP-fel.
    plan.engine_decisions["tp_push"] = _push_to_trainingpeaks(
        plan, zones_profile, client, athlete_user_id
    )


def generate_week(
    athlete_user_id: str,
    week_start: date,
    dry_run: bool = True,
    today: date | None = None,
    week_in_period: int | None = None,
    weeks_in_period: int | None = None,
    locked_workouts: list[ScheduledWorkout] | None = None,
) -> WeekPlan:
    """Generera en veckoplan deterministiskt.

    Args:
        athlete_user_id: auth.users.id (= profiles.id, samma som athlete_profiles.user_id)
        week_start: måndag-datum för veckan
        dry_run: om True, skriv inte till DB
        today: referensdatum för "weeks_until_race"
        week_in_period: manuell override av position i perioden — default None
            → auto från phase_state + adeptens recovery_week_ratio
        weeks_in_period: manuell override av periodlängd (cykellängd)

    Returns:
        WeekPlan med alla beslut spårbara.
    """
    today = today or clock.today()
    client = get_supabase()

    # 1. Hämta adept-data
    athlete = _fetch_athlete(client, athlete_user_id)
    athlete_id = athlete["id"]
    overrides = _fetch_active_overrides(client, athlete_id)
    weekly_report = _fetch_latest_weekly_report(client, athlete_id)
    recent_workouts = _fetch_recent_workouts(
        client, athlete_user_id, weeks_back=4, before=week_start
    )

    # 1b. Nils/adeptens pass i MASTER är auktoritativa. Motorn rör aldrig dagar
    #     som redan har origin nils/manual/legacy-NULL.
    human_rows = _human_planned_sessions(client, athlete_user_id, week_start)
    coached: set[date] = set()
    for row in human_rows:
        try:
            coached.add(date.fromisoformat(str(row.get("date"))[:10]))
        except (TypeError, ValueError):
            continue
    human_workout_count = sum(
        1 for row in human_rows
        if (row.get("sport") or "").strip().lower()
        not in ("vila", "rest", "yoga", "promenad", "vandring")
    )

    # 2. Hämta mastervolym + TP-matad recovery-cache. HRV/sömn/RHR ligger
    # fortfarande i garmin_coach.daily_metrics av kompatibilitetsskäl.
    garmin_id, strava_user_id = _resolve_activity_sources(athlete)
    garmin_metrics = _fetch_garmin_metrics(client, garmin_id, today, days_back=28)
    # Veckovolym från MASTER training_log (user_id), som redan rymmer strava+tp+manuellt.
    actual_weekly_hours = _fetch_actual_weekly_hours(client, athlete_user_id, today, weeks=4)
    if actual_weekly_hours is None and strava_user_id:
        # Strava-adept: härled veckovolym ur strava_activities. (HRV/sömn/RHR
        # saknas i Strava → OT-signaler faller tillbaka på profil/självskattning.)
        actual_weekly_hours = _fetch_strava_weekly_hours(
            client, strava_user_id, today, weeks=4
        )

    # Bygg engine-input
    state = _build_athlete_state(
        athlete, weekly_report, today,
        actual_weekly_hours=actual_weekly_hours,
        garmin_metrics=garmin_metrics,
        client=client,
    )
    ot_signals = _build_ot_signals(athlete, weekly_report, garmin_metrics=garmin_metrics)

    # 3. Kör engine. Fasen behövs FÖRE engine för att resolva veckoposition
    # (vilovecko-cykeln räknas per fas via phase_state).
    phase_rec = determine_phase(state)
    # Fas-override FÖRE positionsräkningen, så att cykel, kategorier,
    # skalning och phase_state alla gäller samma fas.
    phase_rec, phase_honored = _apply_phase_override(phase_rec, overrides)
    week_in_period, weeks_in_period, weeks_in_phase = _resolve_period_position(
        athlete, phase_rec.phase, week_start,
        override_week=week_in_period, override_len=weeks_in_period,
    )
    decisions = _run_engine(
        state, ot_signals, week_in_period, weeks_in_period,
        phase_rec=phase_rec, weeks_in_phase=weeks_in_phase,
    )
    decisions["_weeks_in_period"] = weeks_in_period
    decisions["_week_in_period"] = week_in_period
    decisions["_weeks_in_phase"] = weeks_in_phase
    decisions = _apply_week_volume_scaling(
        decisions, phase_rec.phase, week_in_period, weeks_in_period, weeks_in_phase
    )
    nutrition = _build_nutrition(
        athlete, phase_rec.phase, week_in_period, weeks_in_period
    )
    if nutrition:
        decisions["nutrition"] = nutrition
    decisions, honored = _apply_overrides(decisions, overrides, skip_phase=True)
    honored = phase_honored + honored

    # Spårbarhet: visa vad Trixa ser av TP-cache och utförd mastervolym.
    decisions["_data_sources"] = _trace_data_sources(
        actual_weekly_hours, athlete, garmin_metrics, ot_signals
    )

    # Volym-gap-varning
    if (
        actual_weekly_hours is not None
        and athlete.get("weekly_hours")
        and actual_weekly_hours < float(athlete["weekly_hours"]) * 0.6
    ):
        decisions["_warnings"] = decisions.get("_warnings", []) + [
            f"Faktisk volym ({actual_weekly_hours:.1f}h/v) är mycket lägre"
            f" än deklarerad ({athlete['weekly_hours']}h/v)"
        ]

    phase = decisions["phase_recommendation"]["phase"]
    period = decisions["phase_recommendation"]["period"]
    categories = decisions["categories"]
    discipline_hours = decisions["discipline_hours"]

    # Filtrera till adeptens aktiva discipliner.
    raw_sports = _profile_sports(athlete)
    active_sports = [s for s in raw_sports if s in ("swim", "bike", "run")]

    # Filtrera bort discipliner som blockeras av skada (impact=full)
    impacts = _injury_impacts_per_discipline(athlete)
    blocked_by_injury = [s for s in active_sports if impacts.get(s) == "full"]
    partial_disciplines = {s for s in active_sports if impacts.get(s) == "partial"}
    if blocked_by_injury:
        active_sports = [s for s in active_sports if s not in blocked_by_injury]

    # Re-normalisera discipline_hours: om bike inte är aktivt → ta dess
    # andel och fördela på resterande proportionellt. Gäller även efter
    # att skada blockerat en disciplin.
    discipline_hours = _renormalize_hours(discipline_hours, active_sports)
    decisions["discipline_hours"] = discipline_hours
    decisions["_active_sports"] = active_sports

    # 4. Välj pass från passbanken (utbrutet: _select_week_workouts)
    picked = _select_week_workouts(
        decisions, athlete, athlete_id, week_start, recent_workouts,
        active_sports, partial_disciplines, blocked_by_injury, raw_sports, impacts,
    )
    selected, strength_workout, warnings = picked.selected, picked.strength_workout, picked.warnings
    drills, strength_exercises = picked.drills, picked.strength_exercises
    equipment, preferred_settings = picked.equipment, picked.preferred_settings

    # 5. Schemalägg på dagar — läs adept-preferenser från athlete-row
    long_bike_day = athlete.get("long_bike_day")  # None = "spelar ingen roll"
    long_run_day = athlete.get("long_run_day")
    rest_days_raw = athlete.get("preferred_rest_days") or ["monday"]
    rest_days = rest_days_raw if isinstance(rest_days_raw, list) else ["monday"]

    strength_detail = decisions.get("strength_protocol_detail") or {}
    strength_sessions = (strength_detail.get("sessions_per_week") or [1])[0]

    scheduled = _schedule_workouts(
        selected=selected,
        discipline_hours=discipline_hours,
        week_start=week_start,
        long_bike_day=long_bike_day,
        long_run_day=long_run_day,
        rest_days=rest_days,
        strength_workout=strength_workout,
        locked=locked_workouts,
        reserved_dates=coached,
        existing_workout_count=human_workout_count,
        equipment=equipment,
        preferred_settings=preferred_settings,
        strength_sessions=strength_sessions,
        phase=phase,
        period=period,
    )

    # 5a. GRIND: Nils vinner. Släng motor-genererade pass för dagar coachen
    #     redan lagt — motorn skriver inget för dem. Resten av veckan står kvar.
    if coached:
        skipped = sorted({sw.date for sw in scheduled if sw.date in coached})
        scheduled = [sw for sw in scheduled if sw.date not in coached]
        for d in skipped:
            warnings.append(
                f"{d.isoformat()}: motorn hoppade dagen — coachens plan (Nils) gäller"
            )
    decisions["_coached_dates"] = sorted(d.isoformat() for d in coached)

    combined_workout_count = human_workout_count + sum(
        1 for sw in scheduled if sw.sport != "rest"
    )
    if combined_workout_count < 5:
        warnings.append(
            f"Veckan innehåller bara {combined_workout_count} träningspass efter "
            "skade-, gren- och coachbegränsningar; Trixa lade inte tillbaka blockerade grenar."
        )

    # 5b. Rendera fullständig pass-text per pass (intent + main_set + zoner)
    zones_profile = _build_athlete_profile_for_zones(athlete)
    drill_map = {d["code"]: d for d in drills}
    exercise_map = {e["code"]: e for e in strength_exercises}
    for sw in scheduled:
        if sw.workout_data and sw.sport != "rest":
            try:
                sw.details_markdown = render_workout(
                    sw.workout_data, zones_profile, drill_map, exercise_map
                )
            except Exception:  # noqa: BLE001
                # Render-fel ska inte krascha hela planen — fall back till notes
                sw.details_markdown = sw.notes

    plan = WeekPlan(
        athlete_id=athlete_id,
        athlete_user_id=athlete_user_id,
        week_start=week_start,
        phase=phase,
        period=period,
        week_in_period=week_in_period,
        total_hours_target=state.weekly_training_hours,
        discipline_hours=discipline_hours,
        categories=categories,
        strength_protocol=decisions["strength_protocol"],
        overtraining_level=decisions["overtraining"]["level"],
        overtraining_flags=decisions["overtraining"]["flags"],
        plan_adjustment=decisions.get("plan_adjustment"),
        workouts=scheduled,
        engine_decisions=decisions,
        overrides_honored=honored,
        warnings=warnings,
    )

    # 6. Persist om inte dry-run (utbrutet: _persist_week)
    if not dry_run:
        _persist_week(
            client, plan, athlete, athlete_id, athlete_user_id, week_start,
            honored, phase, period, weeks_in_phase, zones_profile, today,
        )

    return plan


def list_workout_alternatives(
    category: str,
    discipline: str,
    phase: str,
    period: str | None = None,
    exclude_code: str | None = None,
) -> list[dict]:
    """Returnera alla pass i passbanken som matchar samma kategori/disciplin/fas.

    Används för "byt ut passet"-UI:t. Filtrerar bort det aktuella passet
    så adepten inte ser den de redan har.
    """
    phase_filter = _phase_filter_value(phase, period)
    pool = load_workouts()
    return [
        w for w in pool
        if w.get("category") == category
        and w.get("discipline") == discipline
        and phase_filter in (w.get("phase_appropriate") or [])
        and w.get("code") != exclude_code
    ]


def swap_workout_code(
    workout_db_id: str,
    new_code: str,
    user_id: str,
    note: str | None = None,
) -> dict:
    """Byt ut ett specifikt pass i planned_sessions mot ett annat från passbanken.

    Behåller dag och vecka. Uppdaterar title, code, steps, intensity, notes.
    Loggar substitutionen i coach_notes för spårbarhet.

    Returns:
        Den uppdaterade workout-raden.
    """
    client = get_supabase()
    res = (
        client.table("planned_sessions")
        .select("*")
        .eq("id", workout_db_id)
        .eq("user_id", user_id)
        .eq("origin", origins.ENGINE)
        .execute()
    )
    if not res.data:
        raise ValueError(f"planned_sessions-rad saknas: {workout_db_id}")

    new_workout = next(
        (w for w in load_workouts() if w.get("code") == new_code), None
    )
    if new_workout is None:
        raise ValueError(f"Pass saknas i passbanken: {new_code}")
    resolved = (
        resolve_template(new_workout) if new_workout.get("parameterized") else new_workout
    )

    td = resolved.get("total_duration_min") or {}
    duration = int(td.get("estimated") or 60)
    zones = resolved.get("zone_refs") or []
    if resolved.get("discipline") == "strength":
        intensity = f"{resolved.get('type_code', 'styrka')} · RIR-styrt"
    else:
        intensity = ", ".join(str(z) for z in zones) if zones else "Z2"

    details = (resolved.get("intent") or "").strip()
    if note:
        details = f"{details}\nNot: {note}".strip()

    update = {
        "title": resolved.get("name") or new_code,
        "workout_code": new_code,
        "duration_min": duration,
        "intensity": intensity,
        "steps": resolved.get("main_set") or [],
        "details": details,
    }
    upd = (
        client.table("planned_sessions")
        .update(update)
        .eq("id", workout_db_id)
        .eq("user_id", user_id)
        .eq("origin", origins.ENGINE)
        .execute()
    )
    return upd.data[0] if upd.data else {}


def swap_workout_to_next_alternative(
    workout_db_id: str,
    user_id: str,
) -> dict:
    """Byt till nästa deterministiska pass i samma kategori och disciplin."""
    client = get_supabase()
    res = (
        client.table("planned_sessions")
        .select("*")
        .eq("id", workout_db_id)
        .eq("user_id", user_id)
        .eq("origin", origins.ENGINE)
        .execute()
    )
    if not res.data:
        raise ValueError("Passet saknas eller ägs inte av användaren")
    old = res.data[0]
    discipline = sports.canon(old.get("sport"), "other") or "other"
    category = old.get("purpose") or (
        (old.get("workout_code") or "").split("_", 1)[0]
    )

    athlete = _fetch_athlete(client, user_id)
    phase_rec = effective_phase_rec(athlete, client, clock.today())
    phase = phase_rec.phase
    period = phase_rec.period
    candidates = list_workout_alternatives(
        category, discipline, phase, period, old.get("workout_code")
    )
    equipment = athlete.get("equipment") or {}
    settings = athlete.get("preferred_settings") or {}
    candidates = [
        w for w in candidates
        if _passes_equipment_filter(w, equipment)
        and (
            settings.get(discipline, "any") == "any"
            or _pass_setting(w) in (settings.get(discipline), "either")
        )
    ]
    if not candidates:
        raise ValueError("Inget annat pass matchar gren, fas och inställningar")
    chosen = sorted(candidates, key=lambda w: w.get("code") or "")[0]
    return swap_workout_code(
        workout_db_id,
        chosen["code"],
        user_id,
        note=f"Adepten bytte från {old.get('workout_code') or old.get('title')}",
    )


def swap_workout_discipline_and_replan(
    workout_db_id: str,
    new_discipline: str,
    user_id: str,
    new_category: str | None = None,
) -> WeekPlan:
    """Byt en specifik dag till annan disciplin och planera om resten av veckan.

    Steg:
      1. Identifiera vecka och dag från workout_db_id
      2. Välj nytt pass i ny disciplin (samma kategori om inte angiven, annars angiven)
      3. Bygg ScheduledWorkout för den dagen — lås den
      4. Re-kör generate_week med locked_workouts=[lock]

    Returns:
        Den uppdaterade veckoplanen.
    """
    client = get_supabase()
    if new_discipline not in ("swim", "bike", "run", "strength"):
        raise ValueError(f"Ogiltig disciplin: {new_discipline}")
    res = (
        client.table("planned_sessions")
        .select("*")
        .eq("id", workout_db_id)
        .eq("user_id", user_id)
        .eq("origin", origins.ENGINE)
        .execute()
    )
    if not res.data:
        raise ValueError(f"planned_sessions-rad saknas: {workout_db_id}")
    old = res.data[0]

    # user_id finns direkt på planned_sessions; week_start = måndagen för raden.
    athlete_user_id = user_id
    row_date = date.fromisoformat(old["date"]) if isinstance(old["date"], str) else old["date"]
    week_start = row_date - timedelta(days=row_date.weekday())

    # Bestäm kategori — behåll om inte angiven (härled ur workout_code).
    target_category = new_category
    if new_discipline == "strength":
        target_category = "ST"
    if target_category is None:
        old_code = old.get("workout_code") or ""
        target_category = old_code.split("_")[0][:2] if "_" in old_code else "AE"

    # Hämta engine-state för phase
    athlete_full = _fetch_athlete(client, athlete_user_id)
    phase_rec = effective_phase_rec(athlete_full, client, clock.today())
    phase_filter = _phase_filter_value(phase_rec.phase, phase_rec.period)

    # Välj nytt pass — slumpvis från matching
    rng = random.Random(_seed_for(athlete_full["id"], week_start))
    candidates = [
        w for w in load_workouts()
        if w.get("category") == target_category
        and w.get("discipline") == new_discipline
        and phase_filter in (w.get("phase_appropriate") or [])
    ]
    if not candidates:
        # Fall tillbaka till AE-kategori om target inte finns
        candidates = [
            w for w in load_workouts()
            if w.get("category") == "AE"
            and w.get("discipline") == new_discipline
            and phase_filter in (w.get("phase_appropriate") or [])
        ]
    if not candidates:
        raise ValueError(
            f"Inget pass i passbanken matchar {target_category}/{new_discipline}/{phase_filter}"
        )
    new_workout = rng.choice(candidates)

    # Bygg ScheduledWorkout för låsning
    lock_date = date.fromisoformat(old["date"]) if isinstance(old["date"], str) else old["date"]
    locked = _scheduled_from_workout(new_workout, lock_date)

    # Markera audit-not på låst pass
    locked.notes = (
        f"{locked.notes}\n\n[Adept bytte disciplin från {old.get('sport')} → {new_discipline} {clock.today().isoformat()}]"
    ).strip()

    # Re-generera veckan med detta som låst — apply=True skriver över allt
    return generate_week(
        athlete_user_id=athlete_user_id,
        week_start=week_start,
        dry_run=False,
        locked_workouts=[locked],
    )


def _seed_for(athlete_id: str, week_start: date) -> int:
    """Stabil hash för slumpval — samma adept + samma vecka → samma val."""
    raw = f"{athlete_id}:{week_start.isoformat()}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest(), 16) % (2**32)


# ---------- Rendering ----------


def render_plan_markdown(plan: WeekPlan) -> str:
    """Människoläsbar markdown av veckoplanen för terminal/preview."""
    lines: list[str] = []
    lines.append(f"# Veckoplan — {plan.week_start.isoformat()}")
    lines.append("")
    phase_label = plan.phase + (f" ({plan.period})" if plan.period else "")
    lines.append(
        f"**Fas:** {phase_label} — vecka {plan.week_in_period} av {plan.engine_decisions.get('_weeks_in_period', '?')}"
    )
    lines.append(
        f"**Engine-motivering:** {plan.engine_decisions['phase_recommendation']['reason']}"
    )
    lines.append(f"**Total volym (mål):** {plan.total_hours_target:.1f}h")
    lines.append(
        "**Disciplinfördelning:** "
        + ", ".join(f"{d} {h:.1f}h" for d, h in plan.discipline_hours.items())
    )
    lines.append(f"**Kategorier denna vecka:** {', '.join(plan.categories)}")

    strength_detail = plan.engine_decisions.get("strength_protocol_detail")
    if strength_detail:
        spw = strength_detail.get("sessions_per_week") or [1]
        reps = strength_detail.get("reps") or []
        sets = strength_detail.get("sets") or []
        lines.append(
            f"**Styrkeprotokoll:** {plan.strength_protocol} — "
            f"{spw[0]} pass/vecka, {sets[0]}-{sets[-1]} set × {reps[0]}-{reps[-1]} reps"
            f" ({strength_detail.get('intensity', '')})"
        )
    else:
        lines.append(f"**Styrkeprotokoll:** {plan.strength_protocol}")

    recovery = plan.engine_decisions.get("recovery_week") or {}
    if recovery.get("active"):
        lines.append(
            f"**Återhämtningsvecka:** volym skalad till "
            f"{recovery.get('volume_factor', 0.6):.0%} — lätt vecka, testa gärna."
        )
    taper = plan.engine_decisions.get("taper")
    if taper:
        lines.append(
            f"**Taper:** vecka {taper.get('week_in_phase')} i peak — "
            f"volym {taper.get('factor', 1):.0%} av normalvecka."
        )

    nutrition = plan.engine_decisions.get("nutrition")
    if nutrition:
        lines.append("")
        lines.append("## Tävlingsnutrition")
        lines.append(
            f"- Kolhydrater under race: **{nutrition.get('race_carbs_per_hour_g')} g/h**"
            + ("" if nutrition.get("individualized") else " _(generell default — individualisera!)_")
        )
        lines.append(
            f"- Kolhydratladdning: {nutrition.get('carb_load_g_per_kg_per_day')} g/kg/dag, "
            f"{'-'.join(str(d) for d in (nutrition.get('carb_load_days_before') or []))} dagar före"
        )
        lines.append(
            f"- Före start: {nutrition.get('pre_start_intake')} "
            f"~{nutrition.get('pre_start_minutes')} min innan"
        )
        if nutrition.get("notes"):
            lines.append(f"- **Adept-notering:** {nutrition['notes']}")
    lines.append(
        f"**Överträningsbedömning:** {plan.overtraining_level}"
        + (
            f" (flaggor: {', '.join(plan.overtraining_flags)})"
            if plan.overtraining_flags
            else ""
        )
    )

    if plan.plan_adjustment:
        adj = plan.plan_adjustment
        lines.append(
            f"**Planjustering:** -{adj.get('volume_reduction_pct', 0)}% volym, "
            f"-{adj.get('intensity_reduction_pct', 0)}% intensitet, "
            f"+{adj.get('extra_rest_days', 0)} vilodagar"
        )
        if adj.get("consider_medical_consultation"):
            lines.append("> **Överväg läkarkontakt.**")

    if plan.overrides_honored:
        lines.append("")
        lines.append("## Override-respekterade")
        for ov in plan.overrides_honored:
            lines.append(
                f"- {ov.get('scope', '?')}: {ov.get('motivation', '(ingen motivering)')}"
            )

    lines.append("")
    lines.append("## Veckans pass")
    lines.append("")
    for wo in plan.workouts:
        day = wo.date.strftime("%A %Y-%m-%d")
        if wo.sport == "rest":
            lines.append(f"### {day} — Vila")
            lines.append(f"_{wo.notes}_")
        else:
            lines.append(f"### {day} — {wo.title} ({wo.sport})")
            lines.append(
                f"{wo.duration_minutes} min · {wo.intensity} · `{wo.code}`"
            )
            if wo.notes:
                lines.append("")
                lines.append("> " + wo.notes.replace("\n", "\n> "))
        lines.append("")

    if plan.warnings:
        lines.append("## Varningar")
        for w in plan.warnings:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)


# ---------- CLI ----------


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Trixa veckoplan-generator (deterministisk, ingen LLM)."
    )
    parser.add_argument(
        "--athlete-user-id",
        required=True,
        help="auth.users.id för adepten (samma som profiles.id)",
    )
    parser.add_argument(
        "--week-start",
        required=True,
        help="Måndag-datum för veckan, format YYYY-MM-DD",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Skriv till Supabase. Default är dry-run.",
    )
    parser.add_argument(
        "--week-in-period",
        type=int,
        default=None,
        help="Manuell override av position i fas-perioden (1-indexerad). "
        "Default: auto från phase_state + recovery_week_ratio.",
    )
    parser.add_argument(
        "--weeks-in-period",
        type=int,
        default=None,
        help="Manuell override av periodlängd. Default: auto (cykellängd "
        "från recovery_week_ratio, t.ex. 3:1 → 4 veckor).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Skriv ut JSON istället för markdown.",
    )
    args = parser.parse_args()

    try:
        ws = date.fromisoformat(args.week_start)
    except ValueError as exc:
        print(f"Ogiltigt --week-start: {exc}", file=sys.stderr)
        return 2

    try:
        plan = generate_week(
            athlete_user_id=args.athlete_user_id,
            week_start=ws,
            dry_run=not args.apply,
            week_in_period=args.week_in_period,
            weeks_in_period=args.weeks_in_period,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Fel vid generering: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    if args.json:
        print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        print(render_plan_markdown(plan))

    if args.apply:
        persist = plan.engine_decisions.get("planned_sessions_persist") or {}
        print(
            f"\n[Skrev till planned_sessions — {persist.get('written', 0)} pass, "
            f"{persist.get('kept', 0)} behållna, {persist.get('cancelled', 0)} cancel:ade]",
            file=sys.stderr,
        )
    else:
        print("\n[Dry-run — ingen skrivning till DB. Kör med --apply för att skriva.]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
