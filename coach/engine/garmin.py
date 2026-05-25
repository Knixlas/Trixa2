"""Garmin/Supabase-adapter för Trixa2.

Översätter data från Supabase-projektet 'Trixa' (schemat `garmin_coach` + `public`)
till engine-inputs (`AthleteState`, `OvertrainingSignals`).

Designprincip:
- Adaptern är agnostisk till DB-klient. Den tar en `QueryFn`-callable som utför
  en SQL-fråga och returnerar list[dict]. Plugga in psycopg, supabase-py,
  eller MCP execute_sql efter behov.
- Alla SQL-frågor är parametriserade.
- Adaptern läser. Den skriver inte.

Användning:
    from coach.adapters.garmin import build_athlete_state, build_overtraining_signals

    def query(sql: str, params: dict) -> list[dict]:
        # din implementation, ex. psycopg2 cursor.execute()
        ...

    state = build_athlete_state(
        profile_id="09db449d-b8fd-409a-b475-3401b0de9858",
        garmin_athlete_id="98057fa1-4fb9-48f5-be86-b31272dcfed0",
        query=query,
    )

    signals = build_overtraining_signals(
        garmin_athlete_id="98057fa1-4fb9-48f5-be86-b31272dcfed0",
        query=query,
    )

Datakopplingsantagande: `garmin_coach.athlete_profile.user_id` är ännu inte FK
till `public.profiles.id`. När den länkningen är gjord kan båda IDs hämtas
med ett enda anrop i stället för att tas in som parametrar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

from ..engine.phases import AthleteState
from ..engine.overtraining import OvertrainingSignals


# (sql, params) -> list[dict]. Adaptern är agnostisk till klientval.
QueryFn = Callable[[str, dict], list[dict]]


class GarminAdapterError(Exception):
    """Adapter-specifika fel (saknade rader, ogiltig data, etc.)."""


# ---------- AthleteState ----------


def build_athlete_state(
    profile_id: str,
    garmin_athlete_id: str,
    query: QueryFn,
    today: date | None = None,
    weeks_for_avg: int = 4,
) -> AthleteState:
    """Bygg en AthleteState för engine baserat på senaste data.

    Args:
        profile_id: uuid i public.profiles
        garmin_athlete_id: uuid i garmin_coach.activities.athlete_id
        query: callable som utför SQL och returnerar list[dict]
        today: referensdatum (default: idag) — gör testbart
        weeks_for_avg: hur många veckor bakåt för att räkna snitt-veckotimmar

    Returns:
        AthleteState redo för determine_phase().
    """
    today = today or date.today()

    profile = _fetch_profile(profile_id, query)
    if not profile:
        raise GarminAdapterError(f"Ingen profil med id {profile_id}")

    weekly_hours = _avg_weekly_hours(
        garmin_athlete_id, query, today=today, weeks=weeks_for_avg
    )

    weeks_until_race = _weeks_until_race(profile, today)
    has_injury = _has_injury_text(profile)
    has_overtraining_signs = _has_recent_overtraining_signal(profile)
    feels_rested = _feels_rested(garmin_athlete_id, query, today)

    return AthleteState(
        weekly_training_hours=weekly_hours,
        has_injury=has_injury,
        has_overtraining_signs=has_overtraining_signs,
        weeks_until_next_race=weeks_until_race,
        last_race_completed_within_days=None,  # se TODO nedan
        current_phase=None,                    # se TODO nedan
        weeks_in_current_phase=None,
        athlete_feels_rested=feels_rested,
        has_high_specific_fitness=False,       # subjektiv, sätts av coach
    )

    # TODO: current_phase + weeks_in_current_phase ska läsas från
    # public.training_weeks när den tabellen börjar fyllas av Trixa2.
    # TODO: last_race_completed_within_days kräver en races-tabell eller
    # härledas från strava_activities.type='Race' i framtiden.


# ---------- OvertrainingSignals ----------


def build_overtraining_signals(
    garmin_athlete_id: str,
    query: QueryFn,
    today: date | None = None,
    lookback_days: int = 7,
    profile_id: str | None = None,
) -> OvertrainingSignals:
    """Bygg OvertrainingSignals från senaste daily_metrics och activities.

    Mappar Garmins fält direkt till engine-signalerna:
    - resting_hr senaste dygnet vs 7-dagars baseline → rhr_bpm_over_baseline
    - hrv_last_night_ms vs hrv_baseline_low → hrv_pct_below_baseline
    - sleep_score genomsnitt över lookback-fönstret
    - consecutive_low_days fram till idag
    - load_ratio (ACWR) > 1.3 räknas som flera tunga veckor utan vilovecka

    Om `profile_id` anges hämtas också subjektiva signaler från public.profiles:
    - injuries med innehåll → injury_present = True
    - self_assessment ≤ 2 → motivation_low = True
    Dessa är konservativa heuristiker; coach kan sätta dem manuellt om finare
    nyans krävs.
    """
    today = today or date.today()
    metrics = _fetch_recent_metrics(garmin_athlete_id, query, today, lookback_days + 21)

    profile = _fetch_profile(profile_id, query) if profile_id else None

    if not metrics and not profile:
        return OvertrainingSignals()

    # Objektiva signaler från daily_metrics
    rhr_delta = None
    hrv_pct = None
    sleep_avg = None
    consecutive_low = None
    high_load_weeks = None

    if metrics:
        latest = metrics[0]
        last_week = metrics[:lookback_days]
        baseline_pool = metrics[lookback_days:]

        rhr_delta = _rhr_over_baseline(latest, baseline_pool)
        hrv_pct = _hrv_pct_below_baseline(latest)
        sleep_avg = _avg(_collect_nonnull(last_week, "sleep_score"))
        consecutive_low = _count_consecutive_low_sleep(last_week, threshold=60)
        high_load_weeks = _consecutive_high_load_weeks(metrics)

    # Subjektiva signaler från profil
    injury_present = False
    motivation_low = False
    if profile:
        injury_present = _has_injury_text(profile)
        motivation_low = _has_recent_overtraining_signal(profile)

    return OvertrainingSignals(
        rhr_bpm_over_baseline=rhr_delta,
        hrv_pct_below_baseline=hrv_pct,
        sleep_score_avg_7d=sleep_avg,
        sleep_consecutive_low_days=consecutive_low,
        performance_drop_pct=None,
        consecutive_high_load_weeks=high_load_weeks,
        injury_present=injury_present,
        motivation_low=motivation_low,
    )


# ---------- SQL-frågor ----------


def _fetch_profile(profile_id: str, query: QueryFn) -> dict | None:
    sql = """
        SELECT id, name, next_race_name, next_race_date,
               weekly_hours, injuries, health_notes,
               health_notes_updated_at, self_assessment, self_assessment_at,
               resting_hr, ftp, at_hr, max_hr
        FROM public.profiles
        WHERE id = :profile_id
    """
    rows = query(sql, {"profile_id": profile_id})
    return rows[0] if rows else None


def _avg_weekly_hours(
    garmin_athlete_id: str, query: QueryFn, today: date, weeks: int
) -> float:
    """Snitt-träningstimmar per vecka över senaste `weeks` veckor."""
    start = today - timedelta(weeks=weeks)
    sql = """
        SELECT COALESCE(SUM(duration_sec), 0)::float / 3600.0 / :weeks AS avg_hours
        FROM garmin_coach.activities
        WHERE athlete_id = :athlete_id
          AND start_time >= :start_date
          AND start_time < :end_date
    """
    rows = query(sql, {
        "athlete_id": garmin_athlete_id,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
        "weeks": weeks,
    })
    return float(rows[0]["avg_hours"]) if rows else 0.0


def _fetch_recent_metrics(
    garmin_athlete_id: str, query: QueryFn, today: date, days: int
) -> list[dict]:
    """Returnera daily_metrics-rader, nyaste först."""
    start = today - timedelta(days=days)
    sql = """
        SELECT metric_date, resting_hr, hrv_last_night_ms,
               hrv_baseline_low, hrv_baseline_high, hrv_weekly_avg_ms,
               sleep_score, readiness_score, stress_avg,
               acute_load, chronic_load, load_ratio
        FROM garmin_coach.daily_metrics
        WHERE athlete_id = :athlete_id
          AND metric_date >= :start_date
          AND metric_date <= :end_date
        ORDER BY metric_date DESC
    """
    return query(sql, {
        "athlete_id": garmin_athlete_id,
        "start_date": start.isoformat(),
        "end_date": today.isoformat(),
    })


# ---------- Beräkningshjälpare ----------


def _weeks_until_race(profile: dict, today: date) -> int | None:
    raw = profile.get("next_race_date")
    if not raw:
        return None
    try:
        race_date = date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None
    delta_days = (race_date - today).days
    if delta_days < 0:
        return None
    return delta_days // 7


def _has_injury_text(profile: dict) -> bool:
    """True om profiles.injuries har innehåll. Bagatell-skydd: trimmas och
    minst 3 tecken för att räknas (undvik 'nej' eller blank-rader)."""
    text = profile.get("injuries") or ""
    return len(text.strip()) >= 3


def _has_recent_overtraining_signal(profile: dict) -> bool:
    """Konservativ flagga från profiles: låg self_assessment de senaste 14 dagarna."""
    score = profile.get("self_assessment")
    if score is None:
        return False
    return int(score) <= 2  # 1-2 på 1-5-skala räknas som varning


def _feels_rested(
    garmin_athlete_id: str, query: QueryFn, today: date
) -> bool:
    """Heuristik: snitt-readiness senaste 3 dagarna ≥ 75."""
    metrics = _fetch_recent_metrics(garmin_athlete_id, query, today, 3)
    scores = _collect_nonnull(metrics, "readiness_score")
    if not scores:
        return False
    return _avg(scores) >= 75


def _rhr_over_baseline(latest: dict, baseline: list[dict]) -> float | None:
    """Senaste RHR minus median av baseline-pool."""
    latest_rhr = latest.get("resting_hr")
    baseline_values = _collect_nonnull(baseline, "resting_hr")
    if latest_rhr is None or not baseline_values:
        return None
    baseline_median = sorted(baseline_values)[len(baseline_values) // 2]
    return float(latest_rhr - baseline_median)


def _hrv_pct_below_baseline(latest: dict) -> float | None:
    """Hur många procent under hrv_baseline_low senaste natten är.
    Returnerar positiv siffra om värdet är under baseline_low."""
    hrv = latest.get("hrv_last_night_ms")
    baseline_low = latest.get("hrv_baseline_low")
    if hrv is None or baseline_low is None or baseline_low == 0:
        return None
    delta_pct = (float(baseline_low) - float(hrv)) / float(baseline_low) * 100.0
    return max(0.0, delta_pct)  # bara nedsidan räknas som signal


def _count_consecutive_low_sleep(
    metrics: list[dict], threshold: int
) -> int:
    """Räkna dagar i rad från idag bakåt där sleep_score < threshold."""
    streak = 0
    for row in metrics:
        score = row.get("sleep_score")
        if score is None or score >= threshold:
            break
        streak += 1
    return streak


def _consecutive_high_load_weeks(metrics: list[dict]) -> int | None:
    """Räkna sammanhängande veckor med load_ratio > 1.3 (ACWR-tumregel).

    Aggregerar daily load_ratio till veckosnitt. Returnerar None om för få data.
    """
    if len(metrics) < 14:
        return None

    # Gruppera per ISO-vecka, bakifrån (nyaste vecka först)
    by_week: dict[tuple[int, int], list[float]] = {}
    for row in metrics:
        ratio = row.get("load_ratio")
        if ratio is None:
            continue
        d = _to_date(row.get("metric_date"))
        if d is None:
            continue
        iso_year, iso_week, _ = d.isocalendar()
        by_week.setdefault((iso_year, iso_week), []).append(float(ratio))

    if not by_week:
        return None

    # Sortera veckor från nyaste till äldsta
    sorted_weeks = sorted(by_week.keys(), reverse=True)
    streak = 0
    for wk in sorted_weeks:
        avg_ratio = sum(by_week[wk]) / len(by_week[wk])
        if avg_ratio > 1.3:
            streak += 1
        else:
            break
    return streak


# ---------- Småhjälpare ----------


def _collect_nonnull(rows: list[dict], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        v = row.get(key)
        if v is not None:
            out.append(float(v))
    return out


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _to_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
