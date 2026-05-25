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
import random
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any

from coach.engine._loader import load_yaml
from coach.engine.loader import AthleteProfile, load_drills, load_workouts
from coach.engine.overtraining import (
    OvertrainingSignals,
    assess_overtraining,
    recommend_adjustment,
)
from coach.engine.phases import AthleteState, determine_phase
from coach.engine.renderer import render_workout
from coach.engine.strength import current_strength_protocol
from coach.engine.templates import resolve_template
from coach.engine.workouts import (
    distribute_weekly_hours,
    hard_training_cap_minutes,
    max_session_minutes,
    select_workout_types,
)
from coach.trixa.db import get_supabase


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

    def to_db_row(self, athlete_id: str, week_id: str | None) -> dict:
        return {
            "athlete_id": athlete_id,
            "week_id": week_id,
            "date": self.date.isoformat(),
            "sport": self.sport,
            "title": self.title,
            "title_simple": self.code,
            "duration_minutes": self.duration_minutes,
            "intensity": self.intensity,
            "steps": (self.workout_data or {}).get("main_set", []),
            "notes": self.notes,
            "coach_notes": (self.workout_data or {}).get("coach_notes", ""),
            "completed": False,
        }


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


def _fetch_active_overrides(client, athlete_id: str) -> list[dict]:
    res = (
        client.table("coach_overrides")
        .select("*")
        .eq("athlete_id", athlete_id)
        .eq("is_active", True)
        .execute()
    )
    return res.data or []


def _fetch_recent_workouts(client, athlete_id: str, weeks_back: int = 4) -> list[dict]:
    """Hämta passhistorik för variation-constraint i pass-val."""
    since = (date.today() - timedelta(weeks=weeks_back)).isoformat()
    res = (
        client.table("workouts")
        .select("date, sport, title_simple, intensity")
        .eq("athlete_id", athlete_id)
        .gte("date", since)
        .order("date", desc=True)
        .execute()
    )
    return res.data or []


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


# ---------- Bygg engine-state ----------


def _has_significant_injury(athlete: dict) -> bool:
    """Aktiv concern med severity ≥ 3 eller needs_followup → injury."""
    for concern in athlete.get("active_concerns") or []:
        if concern.get("severity", 0) >= 3:
            return True
        if concern.get("needs_followup"):
            return True
    return False


def _weeks_until_race(athlete: dict, today: date) -> int | None:
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


def _build_athlete_state(
    athlete: dict,
    weekly_report: dict | None,
    today: date,
) -> AthleteState:
    """Översätt athlete_profiles + weekly_report → AthleteState."""
    phase_state = athlete.get("phase_state") or {}

    # Self-rapporterad återhämtning från senaste veckorapport
    feels_rested = False
    if weekly_report:
        sleep = weekly_report.get("sleep_quality") or 0
        energy = weekly_report.get("energy") or 0
        feels_rested = sleep >= 4 and energy >= 4

    return AthleteState(
        weekly_training_hours=float(athlete.get("weekly_hours") or 0),
        has_injury=_has_significant_injury(athlete),
        has_overtraining_signs=False,  # härleds från Garmin-data i nästa iteration
        weeks_until_next_race=_weeks_until_race(athlete, today),
        last_race_completed_within_days=None,
        current_phase=phase_state.get("current_phase"),
        weeks_in_current_phase=phase_state.get("weeks_in_phase"),
        athlete_feels_rested=feels_rested,
        has_high_specific_fitness=False,  # subjektiv, coach sätter via override
    )


def _build_ot_signals(
    athlete: dict,
    weekly_report: dict | None,
) -> OvertrainingSignals:
    """Bygg OT-signaler från strukturerade fält. Garmin-data kommer senare."""
    motivation_low = False
    poor_recovery = False
    persistent_fatigue = False
    if weekly_report:
        motivation_low = (weekly_report.get("motivation") or 5) <= 2
        sleep = weekly_report.get("sleep_quality") or 5
        soreness = weekly_report.get("soreness") or 5
        poor_recovery = sleep <= 2 or soreness <= 2
        energy = weekly_report.get("energy") or 5
        persistent_fatigue = energy <= 2

    return OvertrainingSignals(
        motivation_low=motivation_low,
        poor_recovery=poor_recovery,
        muscle_fatigue_persistent=persistent_fatigue,
        injury_present=_has_significant_injury(athlete),
    )


def _build_athlete_profile_for_zones(athlete: dict) -> AthleteProfile:
    """Översätt athlete_profiles → AthleteProfile för zonberäkning."""

    def _parse_swim_css(val: Any) -> float | None:
        """'2:15' → 135.0 sec/100m. Tolererar redan-numeriska värden."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        try:
            parts = str(val).split(":")
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(val)
        except (ValueError, TypeError):
            return None

    def _parse_run_pace(val: Any) -> float | None:
        """'5:15' → 315.0 sec/km."""
        return _parse_swim_css(val)  # samma format

    return AthleteProfile(
        css_sec_per_100m=_parse_swim_css(athlete.get("swim_css")),
        ftp_watts=athlete.get("ftp"),
        lthr_bike_bpm=None,  # finns ej direkt i athlete_profiles ännu
        threshold_pace_sec_per_km=_parse_run_pace(athlete.get("run_threshold_pace")),
        at_hr_run_bpm=athlete.get("lthr"),
        max_hr_bpm=None,
    )


# ---------- Engine-orkestrering ----------


def _run_engine(
    state: AthleteState,
    ot_signals: OvertrainingSignals,
    week_in_period: int,
    weeks_in_period: int,
) -> dict:
    """Kör alla engine-funktioner och samla beslut i en spårbar dict."""
    phase_rec = determine_phase(state)
    categories = select_workout_types(
        phase=phase_rec.phase,
        period=phase_rec.period,
        week_in_period=week_in_period,
        weeks_in_period=weeks_in_period,
    )
    discipline_hours = distribute_weekly_hours(
        phase_rec.phase, state.weekly_training_hours
    )
    hard_cap = hard_training_cap_minutes(
        phase_rec.phase, state.weekly_training_hours * 60
    )
    ot = assess_overtraining(ot_signals)
    adjustment = recommend_adjustment(ot)

    # Strength: undvik fall för faser utan protokoll
    try:
        strength = current_strength_protocol(
            phase=phase_rec.phase,
            period=phase_rec.period,
            week_in_period=week_in_period,
            weeks_in_period=weeks_in_period,
        )
        strength_code = strength.protocol_code
    except ValueError:
        strength_code = "none"

    return {
        "phase_recommendation": {
            "phase": phase_rec.phase,
            "period": phase_rec.period,
            "reason": phase_rec.reason,
            "unmet_criteria": list(phase_rec.unmet_criteria),
        },
        "categories": categories,
        "discipline_hours": discipline_hours,
        "hard_training_cap": hard_cap,
        "overtraining": {
            "level": ot.level,
            "label": ot.label,
            "flag_count": ot.flag_count,
            "flags": list(ot.flags),
        },
        "plan_adjustment": (
            {
                "level": adjustment.level,
                "volume_reduction_pct": adjustment.volume_reduction_pct,
                "intensity_reduction_pct": adjustment.intensity_reduction_pct,
                "extra_rest_days": adjustment.extra_rest_days,
                "swap_to_low_intensity": adjustment.swap_to_low_intensity,
                "consider_medical_consultation": adjustment.consider_medical_consultation,
            }
            if adjustment
            else None
        ),
        "strength_protocol": strength_code,
    }


# ---------- Pass-val ----------


def _phase_filter_value(phase: str, period: str | None) -> str:
    """Konvertera engine-fas/period till värdet som passbankens phase_appropriate
    förväntar (base + base_2 → 'base_2'; prep utan period → 'prep')."""
    return period or phase


def _select_workout_for(
    category: str,
    discipline: str,
    phase_filter: str,
    workouts_pool: list[dict],
    recent_codes: set[str],
    rng: random.Random,
) -> dict | None:
    """Välj ett pass för given kategori + disciplin. Föredrar pass som inte
    körts senaste 4 veckorna; faller tillbaka till hela poolen om alla har körts."""
    candidates = [
        w
        for w in workouts_pool
        if w.get("category") == category
        and w.get("discipline") == discipline
        and phase_filter in (w.get("phase_appropriate") or [])
    ]
    if not candidates:
        return None

    fresh = [w for w in candidates if w.get("code") not in recent_codes]
    pool = fresh or candidates
    return rng.choice(pool)


def _pick_long_workout_duration(
    workout: dict,
    discipline_hours: float,
    is_long_day: bool,
) -> int:
    """Bestäm duration för parameterized pass.

    Heuristik:
      - Långpass-dag → max-spannet inom rimliga gränser (~50% av disciplinens veckotid)
      - Annars → default-värdet
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
            return min(target, cap)
        return int(default)

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
    strength_code: str,
    locked: list[ScheduledWorkout] | None = None,
    include_strength: bool = True,
) -> list[ScheduledWorkout]:
    """Fördela utvalda pass över veckodagar.

    Schemaprinciper (i ordning):
      1. Vilodagar låses från `preferred_rest_days` (eller default måndag).
      2. Långpass-bike (största AE bike) på `long_bike_day` (default lördag).
      3. Långpass-löp (största AE run) på `long_run_day` (default söndag).
      4. Kvalitetspass (ME/AC/MF) — använd båda-grannar-constraint:
         placera inte samma disciplin på dag före ELLER dag efter.
      5. Speed (SS) — samma constraint.
      6. Volym (AE som inte är långpass) — samma constraint.
      7. Strength — på en kvalitetsdag som är ledig.

    Brick: BW-kategori = bike+run kombo. När det finns konkreta brick-pass i
    passbanken (filer brick_*.yaml) placeras de på long_bike_day eftersom
    brick ÄR bike följt av run i samma pass — inte separata dagar.
    """
    day_dates = {day: week_start + timedelta(days=_DAY_INDEX[day]) for day in _DAYS}
    schedule: dict[str, ScheduledWorkout] = {}

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
        if cat in ("ME", "AC", "MF"):
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

        # Om brick-pass finns och disciplin är bike — föredra brick istället
        bike_options = volume + (brick_pool if discipline == "bike" else [])
        run_options = volume

        pool = bike_options if discipline == "bike" else run_options
        disc_pool = [w for w in pool if w.get("discipline") in (discipline, "brick")]
        if not disc_pool:
            return

        chosen = max(disc_pool, key=_estimated_duration_minutes)

        for d in candidates:
            if d in schedule:
                continue
            # Tillåt långpass även om granne är samma disciplin —
            # långpasset är kärnan, det måste placeras
            schedule[d] = _scheduled_from_workout(chosen, day_dates[d], is_long=True)
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
                    schedule[d] = _scheduled_from_workout(w, day_dates[d])
                    pass_list.remove(w)
                    placed = True
                    break
            if not placed:
                # Inget perfekt val — ta första lediga dag
                for d in _DAYS:
                    if d not in schedule:
                        schedule[d] = _scheduled_from_workout(w, day_dates[d])
                        pass_list.remove(w)
                        break

    # Ordning: kvalitet (hårdast → prio), sen speed (lätta tillägg), sen volym, sen övrigt
    place_with_neighbor_constraint(quality)
    place_with_neighbor_constraint(speed)
    place_with_neighbor_constraint(volume)
    place_with_neighbor_constraint(other)
    place_with_neighbor_constraint(brick_pool)  # om kvar (oplacerade brick)

    # 7. Strength på en ledig kvalitetsdag (om aktiverad)
    if include_strength and strength_code and strength_code != "none":
        for d in ("wednesday", "friday", "tuesday", "thursday"):
            if d not in schedule:
                schedule[d] = ScheduledWorkout(
                    date=day_dates[d],
                    sport="strength",
                    code=f"strength_{strength_code}",
                    title=f"Styrka — {strength_code}",
                    category="STR",
                    duration_minutes=45,
                    intensity=strength_code,
                    notes=f"Protokoll: {strength_code}. Se data/strength.yaml för övningar och reps.",
                )
                break

    return [schedule[d] for d in _DAYS if d in schedule]


def _scheduled_from_workout(
    workout: dict,
    dt: date,
    is_long: bool = False,
) -> ScheduledWorkout:
    """Konvertera passbankens workout-dict → ScheduledWorkout."""
    resolved = (
        resolve_template(workout, {"duration_min": _pick_long_workout_duration(workout, 6.0, is_long)})
        if workout.get("parameterized")
        else workout
    )
    td = resolved.get("total_duration_min") or {}
    duration = int(td.get("estimated") or 60)

    zones = resolved.get("zone_refs") or []
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


# ---------- Skriv till DB ----------


def _ensure_training_plan(
    client, athlete_id: str, race_name: str, race_date: str | None
) -> str:
    """Hitta aktiv training_plan eller skapa en. Returnerar plan_id."""
    res = (
        client.table("training_plans")
        .select("id")
        .eq("athlete_id", athlete_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["id"]

    # Räkna ut total_weeks från race_date om möjligt
    total_weeks = 16
    if race_date:
        try:
            rd = date.fromisoformat(str(race_date)[:10])
            total_weeks = max(1, (rd - date.today()).days // 7)
        except (ValueError, TypeError):
            pass

    create = (
        client.table("training_plans")
        .insert(
            {
                "athlete_id": athlete_id,
                "race_name": race_name or "Saknas",
                "race_date": race_date,
                "total_weeks": total_weeks,
                "current_week": 1,
                "is_active": True,
            }
        )
        .execute()
    )
    return create.data[0]["id"]


def _upsert_training_week(
    client, plan_id: str, week_start: date, phase: str
) -> str:
    """Skapa eller hitta training_weeks-rad för veckan. Returnerar week_id."""
    iso_year, iso_week, _ = week_start.isocalendar()
    res = (
        client.table("training_weeks")
        .select("id")
        .eq("plan_id", plan_id)
        .eq("week_number", iso_week)
        .eq("year", iso_year)
        .execute()
    )
    if res.data:
        week_id = res.data[0]["id"]
        # Rensa gamla pass för veckan (idempotens)
        client.table("workouts").delete().eq("week_id", week_id).execute()
        return week_id

    create = (
        client.table("training_weeks")
        .insert(
            {
                "plan_id": plan_id,
                "week_number": iso_week,
                "year": iso_year,
                "phase": phase,
            }
        )
        .execute()
    )
    return create.data[0]["id"]


def _persist_plan(client, plan: WeekPlan, race_name: str, race_date: str | None) -> str:
    """Skriv hela planen till DB. Returnerar week_id."""
    plan_id = _ensure_training_plan(client, plan.athlete_id, race_name, race_date)
    week_id = _upsert_training_week(client, plan_id, plan.week_start, plan.phase)

    rows = [wo.to_db_row(plan.athlete_id, week_id) for wo in plan.workouts if wo.sport != "rest"]
    if rows:
        client.table("workouts").insert(rows).execute()

    return week_id


# ---------- Override-hantering ----------


def _apply_overrides(
    engine_decisions: dict,
    overrides: list[dict],
) -> tuple[dict, list[dict]]:
    """Applicera aktiva coach_overrides på engine-beslut.

    Returnerar (modified_decisions, honored_list).
    """
    modified = dict(engine_decisions)
    honored: list[dict] = []

    for ov in overrides:
        scope = ov.get("scope")
        decision = ov.get("override_decision") or {}
        if scope == "phase" and decision.get("phase"):
            modified["phase_recommendation"] = {
                **modified["phase_recommendation"],
                "phase": decision["phase"],
                "period": decision.get("period"),
                "reason": f"Override: {ov.get('motivation', '')}",
            }
            honored.append(ov)
        elif scope == "volume" and decision.get("weekly_hours"):
            new_hours = float(decision["weekly_hours"])
            phase = modified["phase_recommendation"]["phase"]
            modified["discipline_hours"] = distribute_weekly_hours(phase, new_hours)
            honored.append(ov)
        elif scope == "overtraining" and decision.get("level"):
            modified["overtraining"] = {
                **modified["overtraining"],
                "level": decision["level"],
            }
            honored.append(ov)
        # week/workout-overrides hanteras inte här — de gäller specifika rader
        # och tillämpas av Nils direkt på workouts-tabellen.

    return modified, honored


# ---------- Huvudfunktion ----------


def generate_week(
    athlete_user_id: str,
    week_start: date,
    dry_run: bool = True,
    today: date | None = None,
    week_in_period: int = 1,
    weeks_in_period: int = 6,
    locked_workouts: list[ScheduledWorkout] | None = None,
) -> WeekPlan:
    """Generera en veckoplan deterministiskt.

    Args:
        athlete_user_id: auth.users.id (= profiles.id, samma som athlete_profiles.user_id)
        week_start: måndag-datum för veckan
        dry_run: om True, skriv inte till DB
        today: referensdatum för "weeks_until_race"
        week_in_period: 1-indexerad position i nuvarande fas-period
        weeks_in_period: total längd på nuvarande period

    Returns:
        WeekPlan med alla beslut spårbara.
    """
    today = today or date.today()
    client = get_supabase()

    # 1. Hämta adept-data
    athlete = _fetch_athlete(client, athlete_user_id)
    athlete_id = athlete["id"]
    overrides = _fetch_active_overrides(client, athlete_id)
    weekly_report = _fetch_latest_weekly_report(client, athlete_id)
    recent_workouts = _fetch_recent_workouts(client, athlete_id, weeks_back=4)

    # 2. Bygg engine-input
    state = _build_athlete_state(athlete, weekly_report, today)
    ot_signals = _build_ot_signals(athlete, weekly_report)

    # 3. Kör engine
    decisions = _run_engine(state, ot_signals, week_in_period, weeks_in_period)
    decisions["_weeks_in_period"] = weeks_in_period
    decisions, honored = _apply_overrides(decisions, overrides)

    phase = decisions["phase_recommendation"]["phase"]
    period = decisions["phase_recommendation"]["period"]
    categories = decisions["categories"]
    discipline_hours = decisions["discipline_hours"]

    # Filtrera till adeptens aktiva discipliner
    raw_sports = athlete.get("sports") or ["swim", "bike", "run"]
    active_sports = [s for s in raw_sports if s in ("swim", "bike", "run")]
    if not active_sports:
        # Säkerhetsnät — om listan är tom: fall tillbaka till alla tre
        active_sports = ["swim", "bike", "run"]

    # Re-normalisera discipline_hours: om bike inte är aktivt → ta dess
    # andel och fördela på resterande proportionellt.
    discipline_hours = _renormalize_hours(discipline_hours, active_sports)
    decisions["discipline_hours"] = discipline_hours
    decisions["_active_sports"] = active_sports

    # 4. Välj pass från passbanken
    workouts_pool = load_workouts()
    drills = load_drills()  # noqa: F841 — används av render om vi vill rendra här
    recent_codes = {w.get("title_simple") for w in recent_workouts if w.get("title_simple")}
    rng = random.Random(_seed_for(athlete_id, week_start))
    phase_filter = _phase_filter_value(phase, period)

    selected: list[dict] = []
    warnings: list[str] = []
    for cat in categories:
        for disc in active_sports:
            # BW (brick) finns bara som disciplin "brick" eller "bike"+"run"-kombination
            if cat == "BW" and disc != "bike":
                continue
            chosen = _select_workout_for(
                cat, disc, phase_filter, workouts_pool, recent_codes, rng
            )
            if chosen is None:
                warnings.append(
                    f"Inget pass i passbanken matchar {cat} + {disc} + {phase_filter}"
                )
                continue
            selected.append(chosen)

    # 5. Schemalägg på dagar — läs adept-preferenser från athlete-row
    long_bike_day = athlete.get("long_bike_day")  # None = "spelar ingen roll"
    long_run_day = athlete.get("long_run_day")
    rest_days_raw = athlete.get("preferred_rest_days") or ["monday"]
    rest_days = rest_days_raw if isinstance(rest_days_raw, list) else ["monday"]

    scheduled = _schedule_workouts(
        selected=selected,
        discipline_hours=discipline_hours,
        week_start=week_start,
        long_bike_day=long_bike_day,
        long_run_day=long_run_day,
        rest_days=rest_days,
        strength_code=decisions["strength_protocol"],
        locked=locked_workouts,
        include_strength="strength" in raw_sports,
    )

    # 5b. Rendera fullständig pass-text per pass (intent + main_set + zoner)
    zones_profile = _build_athlete_profile_for_zones(athlete)
    drill_map = {d["code"]: d for d in drills}
    for sw in scheduled:
        if sw.workout_data and sw.sport not in ("rest", "strength"):
            try:
                sw.details_markdown = render_workout(
                    sw.workout_data, zones_profile, drill_map
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

    # 6. Persist om inte dry-run
    if not dry_run:
        week_id = _persist_plan(
            client,
            plan,
            race_name=athlete.get("race_type") or "Ironman",
            race_date=athlete.get("race_date"),
        )
        plan.engine_decisions["persisted_week_id"] = week_id

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
    note: str | None = None,
) -> dict:
    """Byt ut ett specifikt pass i workouts-tabellen mot ett annat från passbanken.

    Behåller dag och vecka. Uppdaterar title, code, steps, intensity, notes.
    Loggar substitutionen i coach_notes för spårbarhet.

    Returns:
        Den uppdaterade workout-raden.
    """
    client = get_supabase()
    res = client.table("workouts").select("*").eq("id", workout_db_id).execute()
    if not res.data:
        raise ValueError(f"Workout saknas: {workout_db_id}")
    old = res.data[0]

    # Hitta nytt pass i passbanken
    new_workout = next(
        (w for w in load_workouts() if w.get("code") == new_code), None
    )
    if new_workout is None:
        raise ValueError(f"Pass saknas i passbanken: {new_code}")

    # Resolva ev. parameterized template
    resolved = (
        resolve_template(new_workout) if new_workout.get("parameterized") else new_workout
    )

    # Rendera fullständig text för audit
    audit_line = (
        f"[{date.today().isoformat()}] Substituerat från "
        f"{old.get('title_simple', '?')} → {new_code} av adept."
    )
    existing_notes = (old.get("coach_notes") or "").strip()
    new_coach_notes = (
        f"{existing_notes}\n{audit_line}" if existing_notes else audit_line
    )

    td = resolved.get("total_duration_min") or {}
    duration = int(td.get("estimated") or 60)
    zones = resolved.get("zone_refs") or []
    intensity = ", ".join(str(z) for z in zones) if zones else "Z2"

    update = {
        "title": resolved.get("name") or new_code,
        "title_simple": new_code,
        "duration_minutes": duration,
        "intensity": intensity,
        "steps": resolved.get("main_set") or [],
        "notes": (resolved.get("intent") or "").strip(),
        "coach_notes": new_coach_notes,
    }
    if note:
        update["coach_notes"] = f"{new_coach_notes}\nNot: {note}"

    upd = client.table("workouts").update(update).eq("id", workout_db_id).execute()
    return upd.data[0] if upd.data else {}


def swap_workout_discipline_and_replan(
    workout_db_id: str,
    new_discipline: str,
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
    w_res = client.table("workouts").select("*").eq("id", workout_db_id).execute()
    if not w_res.data:
        raise ValueError(f"Workout saknas: {workout_db_id}")
    old = w_res.data[0]

    # Hämta vecka för att veta week_start och athlete
    week_res = (
        client.table("training_weeks")
        .select("id, year, week_number")
        .eq("id", old["week_id"])
        .execute()
    )
    if not week_res.data:
        raise ValueError("training_weeks-rad saknas för workout")
    week = week_res.data[0]
    week_start = date.fromisocalendar(week["year"], week["week_number"], 1)

    # Hämta athlete user_id via athlete_id
    a_res = (
        client.table("athlete_profiles")
        .select("user_id")
        .eq("id", old["athlete_id"])
        .execute()
    )
    if not a_res.data:
        raise ValueError("athlete_profiles saknas")
    athlete_user_id = a_res.data[0]["user_id"]

    # Bestäm kategori — behåll om inte angiven
    target_category = new_category
    if target_category is None:
        # Försök härleda från gamla passets title_simple
        old_code = old.get("title_simple") or ""
        # Format: AE2_swim_01 → AE
        target_category = old_code.split("_")[0][:2] if "_" in old_code else "AE"

    # Hämta engine-state för phase
    athlete_full = _fetch_athlete(client, athlete_user_id)
    state = _build_athlete_state(athlete_full, None, date.today())
    decisions = _run_engine(state, _build_ot_signals(athlete_full, None), 1, 6)
    phase = decisions["phase_recommendation"]["phase"]
    period = decisions["phase_recommendation"]["period"]
    phase_filter = _phase_filter_value(phase, period)

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
        f"{locked.notes}\n\n[Adept bytte disciplin från {old.get('sport')} → {new_discipline} {date.today().isoformat()}]"
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
    lines.append(f"**Styrkeprotokoll:** {plan.strength_protocol}")
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
                f"`{wo.code}` | {wo.category} | {wo.duration_minutes} min | {wo.intensity}"
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
        default=1,
        help="Position i nuvarande fas-period (1-indexerad).",
    )
    parser.add_argument(
        "--weeks-in-period",
        type=int,
        default=6,
        help="Total längd på nuvarande fas-period.",
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
        print(
            f"\n[Skrev till DB — week_id={plan.engine_decisions.get('persisted_week_id')}]",
            file=sys.stderr,
        )
    else:
        print("\n[Dry-run — ingen skrivning till DB. Kör med --apply för att skriva.]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(_cli())
