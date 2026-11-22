"""Nivåprofiler: veckobudget, passtak och gå/jogg-steg per erfarenhetsnivå.

experience_level lästes inte av planeraren förrän 2026-11-22 (docs/13). En
nybörjare som angav 0,5 h/vecka fick fem pass och 5–6 h, för att golvet på
5 h och "minst fem pass" gällde alla. Profilerna bor i data/levels.yaml.

Två modeller:
  declared — dagens logik, för advanced/elite (och okänd nivå).
  ramp     — budget i minuter som börjar lågt och växer med högst 10 % när
             förra veckan blev gjord. Angivna timmar är ett tak.

Trixa-lagret: läser data, räknar, skriver inget. Planeraren sparar
rampens tillstånd i phase_state["ramp"].
"""

from __future__ import annotations

from datetime import date
from typing import Any

from coach.engine._loader import load_yaml

_FALLBACK_LEVEL = "advanced"   # okänd nivå → dagens beteende

# Loppdistans → gren för själva loppdagen. Triathlon ligger som brick
# (TP:s ett-pass-brick), hinderbana som löpning.
_RACE_SPORT = {
    "5k": "run", "10k": "run", "half_marathon": "run", "marathon": "run", "ultra": "run",
    "obstacle_sprint": "run", "obstacle_standard": "run", "obstacle_ultra": "run",
    "gran_fondo": "bike", "time_trial": "bike", "stage_race": "bike",
    "open_water": "swim", "swim_meet": "swim",
    "sprint": "brick", "olympic": "brick", "half": "brick", "full": "brick",
    "ironman": "brick",
}

# Ungefärlig lopptid i minuter när adepten inte angett måltid.
_RACE_ESTIMATE_MIN = {
    "5k": 35, "10k": 70, "half_marathon": 150, "marathon": 300, "ultra": 480,
    "obstacle_sprint": 60, "obstacle_standard": 120, "obstacle_ultra": 300,
    "gran_fondo": 600, "time_trial": 60, "stage_race": 240,
    "open_water": 60, "swim_meet": 30,
    "sprint": 100, "olympic": 180, "half": 360, "full": 780, "ironman": 780,
}


def _config() -> dict:
    return load_yaml("levels.yaml")


def profile(athlete: dict) -> dict:
    """Nivåprofilen för adepten, med nyckeln ``level`` ifylld."""
    levels = _config()["levels"]
    level = athlete.get("experience_level")
    if level not in levels:
        level = _FALLBACK_LEVEL
    return {"level": level, **levels[level]}


def is_ramp(prof: dict) -> bool:
    return prof.get("model") == "ramp"


def race_sport(distance: str | None) -> str:
    return _RACE_SPORT.get(distance or "", "run")


def race_estimate_minutes(race: dict) -> int:
    """Måltiden om adepten angett en, annars en uppskattning per distans."""
    target = str(race.get("target_total") or "").strip()
    if target:
        try:
            parts = [int(p) for p in target.split(":")]
            while len(parts) < 3:
                parts.insert(0, 0)
            h, m, s = parts[-3:]
            minutes = h * 60 + m + (1 if s >= 30 else 0)
            if minutes > 0:
                return minutes
        except ValueError:
            pass
    return _RACE_ESTIMATE_MIN.get(race.get("distance") or "", 60)


def session_cap(prof: dict, discipline: str | None, race_distance: str | None) -> int | None:
    """Längsta pass för grenen: nivåns tak, och loppets tak i loppets gren."""
    caps = (prof.get("max_session_min") or {})
    cap = caps.get(discipline or "")
    if is_ramp(prof) and race_distance and race_sport(race_distance) in (discipline, "brick"):
        race_cap = (_config().get("race_long_cap_min") or {}).get(race_distance)
        if race_cap:
            cap = min(cap, race_cap) if cap else race_cap
    return cap


def taper_factor(weeks_until_race: int | None) -> float | None:
    """Andel av veckans volym de sista veckorna före loppet (alla nivåer)."""
    if weeks_until_race is None:
        return None
    table = _config().get("race_taper") or {}
    value = table.get(weeks_until_race)
    if value is None:
        value = table.get(str(weeks_until_race))
    return float(value) if value is not None else None


def allowed_categories(prof: dict, categories: list[str], progress_weeks: int) -> list[str]:
    """Filtrera fasens kategorier mot nivån.

    Nybörjaren får bara lugn uthållighet (AE) tills ``speed_after_progress_weeks``
    gjorda veckor; därefter också korta fartökningar (SS). Aldrig test,
    tröskel eller intervaller.
    """
    allowed = prof.get("allowed_categories")
    if allowed is None:
        return list(categories)
    allowed = set(allowed)
    if progress_weeks >= int(prof.get("speed_after_progress_weeks") or 0) > 0:
        allowed.add("SS")
    return [c for c in categories if c in allowed]


def run_walk(prof: dict, progress_weeks: int) -> tuple[float, float] | None:
    """(jogg, gång) i minuter för gå/jogg-passet, eller None när steget är passerat."""
    steps = prof.get("run_walk_steps") or []
    if not steps:
        return None
    per = max(1, int(prof.get("progress_weeks_per_step") or 2))
    idx = progress_weeks // per
    if idx >= len(steps):
        return None
    jog, walk = steps[idx]
    return float(jog), float(walk)


def sessions_for_budget(prof: dict, budget_min: float) -> int:
    """Antal konditionspass budgeten räcker till: minst ett, högst nivåns tak."""
    max_sessions = int(prof.get("max_sessions") or 3)
    per_session = max(int(prof.get("min_session_min") or 15), 25)
    return max(1, min(max_sessions, int(budget_min // per_session)))


def weekly_budget(
    prof: dict,
    ramp_state: dict | None,
    week_start: date,
    declared_hours: float | None,
    last_week_actual_min: float | None,
    actual_avg_hours: float | None,
    hold_reason: str | None = None,
) -> dict[str, Any]:
    """Veckobudget i minuter för en ramp-nivå, och nytt rampläge.

    ramp_state är phase_state["ramp"] från förra planeringen:
      week_start, basis_minutes, budget_minutes, progress_basis,
      progress_weeks, scheduled_minutes, recovery.
    Körs samma vecka igen räknas det från ``basis`` så att budgeten inte
    växer för varje regenerering.

    Förra veckans gjorda andel = faktisk tid / schemalagd tid:
      ≥ hold_if_completion_below   → +max_weekly_increase, ett steg framåt
      ≥ reduce_if_completion_below → samma budget
      lägre                        → × reduce_factor
    Angivna timmar är ett tak. Under en vilovecka växer inte basen.
    """
    start = float(prof.get("start_minutes_per_week") or 75)
    inc = float(prof.get("max_weekly_increase") or 0.10)
    hold = float(prof.get("hold_if_completion_below") or 0.75)
    reduce = float(prof.get("reduce_if_completion_below") or 0.5)
    reduce_factor = float(prof.get("reduce_factor") or 0.9)
    floor = float(prof.get("min_session_min") or 15)

    rs = dict(ramp_state or {})
    rerun = rs.get("week_start") == week_start.isoformat()
    completion: float | None = None
    reason: str

    if rerun:
        prev = rs.get("basis_minutes")
        progress = int(rs.get("progress_basis") or 0)
        prev_sched = rs.get("basis_scheduled_minutes")
        prev_recovery = bool(rs.get("basis_recovery"))
    else:
        prev = rs.get("budget_minutes")
        progress = int(rs.get("progress_weeks") or 0)
        prev_sched = rs.get("scheduled_minutes")
        prev_recovery = bool(rs.get("recovery"))

    if prev is None:
        budget = start
        # Den som redan tränar (medel, comeback med logg) börjar där hen är.
        if prof.get("level") != "beginner" and actual_avg_hours:
            budget = max(start, actual_avg_hours * 60)
        reason = f"Första veckan: {round(budget)} min"
    else:
        prev = float(prev)
        # Väx från det som faktiskt gick att schemalägga, inte från en budget
        # som passtaken aldrig lät veckan nå. Annars klättrar budgeten till
        # adeptens tak medan veckan står still, och nedtrappningen räknas
        # från en volym som aldrig fanns. Lugna veckor är medvetet kortare —
        # där gäller budgeten.
        if prev_sched and not prev_recovery:
            prev = min(prev, float(prev_sched))
        if prev_sched and last_week_actual_min is not None:
            completion = float(last_week_actual_min) / float(prev_sched)
        if completion is None:
            budget, reason = prev, "Ingen logg förra veckan — samma budget"
        elif completion >= hold and hold_reason:
            # Besvär: veckan blev gjord, men varken mängd eller steg ökar.
            budget, reason = prev, hold_reason
        elif completion >= hold:
            budget = prev if prev_recovery else prev * (1 + inc)
            progress += 1
            reason = (f"Förra veckan gjord ({completion:.0%})"
                      + (" — lugn vecka, basen står still" if prev_recovery
                         else f" — +{inc:.0%}"))
        elif completion >= reduce:
            budget, reason = prev, f"Förra veckan till {completion:.0%} — samma budget"
        else:
            budget = prev * reduce_factor
            reason = f"Förra veckan till {completion:.0%} — budgeten sänks"

    capped = False
    if declared_hours and declared_hours > 0 and budget > declared_hours * 60:
        budget = declared_hours * 60
        capped = True
    budget = max(floor, round(budget))

    new_state = {
        "week_start": week_start.isoformat(),
        "basis_minutes": prev,
        "progress_basis": int(rs.get("progress_basis") or 0) if rerun else int(rs.get("progress_weeks") or 0),
        "basis_scheduled_minutes": prev_sched,
        "basis_recovery": prev_recovery,
        "budget_minutes": budget,
        "progress_weeks": progress,
    }
    return {
        "budget_minutes": budget,
        "progress_weeks": progress,
        "completion": completion,
        "capped_by_declared": capped,
        "reason": reason,
        "state": new_state,
    }
