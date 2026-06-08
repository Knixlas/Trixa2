"""Strukturera Nils fritext-pass i planned_sessions → konkreta steps.

Nils planerar med fritext (titel + sport + duration), ofta utan `steps`. Sådana
rader kan inte pushas till TrainingPeaks som strukturerade pass. Den här modulen
fyller dem deterministiskt:

    fritext-rad  →  resolve_session() (regeltabell)  →  passbankskod
                 →  resolve_template(kod, duration)   →  steps (main_set)
                 →  uppdatera planned_sessions additivt (workout_code + steps)

Respekterar en `workout_code` som Nils redan satt (resolverar bara dess steps).
Matchar ingen regel → raden lämnas orörd och rapporteras (eskalering, inte
gissning). Push till TP sker separat och idempotent
(workout_writer.sync_planned_week_to_tp); eller kör hela kedjan via
`python -m coach.trixa.sync_week --apply`.

CLI:
    python -m coach.trixa.structure_sessions --week-start 2026-06-15 [--apply]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from coach.engine.templates import resolve_template
from coach.trixa.session_mapping import (
    discipline_for_sport,
    load_session_mapping,
    resolve_session,
)

DEFAULT_USER_ID = "09db449d-b8fd-409a-b475-3401b0de9858"


@dataclass
class StructureResult:
    to_update: list[dict] = field(default_factory=list)   # {id,date,sport,title,code,steps,source}
    unmatched: list[dict] = field(default_factory=list)   # {id,date,sport,title,duration_min}
    skipped: list[dict] = field(default_factory=list)      # {id,date,reason}


def _steps_for(code: str, duration_min: float | None, pool: dict) -> list[dict]:
    """Resolva passets main_set för given duration (default om duration saknas)."""
    w = pool[code]
    params = {"duration_min": duration_min} if duration_min else None
    resolved = resolve_template(w, params) if w.get("parameterized") else w
    return list(resolved.get("main_set", []))


def structure_rows(rows: list[dict], pool: dict, rules: list[dict] | None = None) -> StructureResult:
    """Ren kärna: klassificera rader och bygg steps. Ingen DB.

    Args:
        rows: planned_sessions-rader (id, date, sport, title, workout_code,
              duration_min, steps).
        pool: {code: workout}.
        rules: regellista; laddas från YAML om None.
    """
    if rules is None:
        rules = load_session_mapping()
    res = StructureResult()
    for r in rows:
        rid = r.get("id")
        d = str(r.get("date") or "")[:10]
        sport = r.get("sport")
        title = r.get("title") or ""
        duration = r.get("duration_min")
        discipline = discipline_for_sport(sport)

        if discipline in ("rest", ""):
            res.skipped.append({"id": rid, "date": d, "reason": f"sport={sport}"})
            continue
        if r.get("steps"):
            res.skipped.append({"id": rid, "date": d, "reason": "har redan steps"})
            continue

        # Respektera en kod Nils redan satt; annars regelmatcha titeln.
        existing_code = r.get("workout_code")
        if existing_code and existing_code in pool:
            code, source = existing_code, "workout_code"
        else:
            code, source = resolve_session(sport, title, duration or 60, pool, rules)

        if not code:
            res.unmatched.append({
                "id": rid, "date": d, "sport": sport,
                "title": title, "duration_min": duration,
            })
            continue

        res.to_update.append({
            "id": rid, "date": d, "sport": sport, "title": title,
            "code": code, "duration_min": duration, "source": source,
            "steps": _steps_for(code, duration, pool),
        })
    return res


def structure_week(
    pg: Any,
    user_id: str,
    week_start: date,
    pool: dict,
    rules: list[dict] | None = None,
    apply: bool = False,
) -> StructureResult:
    """Läs veckans planned_sessions, strukturera fritext-rader, skriv om apply."""
    week_end = (week_start + timedelta(days=6)).isoformat()
    rows = (
        pg.table("planned_sessions")
        .select("id, date, sport, title, workout_code, duration_min, steps")
        .eq("user_id", user_id)
        .gte("date", week_start.isoformat())
        .lte("date", week_end)
        .order("date")
        .execute()
    ).data or []

    res = structure_rows(rows, pool, rules)

    if apply:
        for u in res.to_update:
            (pg.table("planned_sessions")
                .update({"workout_code": u["code"], "steps": u["steps"]})
                .eq("id", u["id"])
                .execute())
    return res


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strukturera Nils fritext-pass → steps.")
    parser.add_argument("--user", default=DEFAULT_USER_ID)
    parser.add_argument("--week-start", default=None,
                        help="YYYY-MM-DD (default: innevarande veckas måndag)")
    parser.add_argument("--apply", action="store_true",
                        help="skriv workout_code + steps till planned_sessions")
    args = parser.parse_args(argv)

    from coach.engine.loader import load_workouts
    from coach.trixa.db import get_postgrest

    week_start = (
        date.fromisoformat(args.week_start) if args.week_start
        else _monday_of(date.today())
    )
    pool = {w["code"]: w for w in load_workouts()}
    pg = get_postgrest()
    res = structure_week(pg, args.user, week_start, pool, apply=args.apply)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] structure_week {week_start} → {week_start + timedelta(days=6)}")
    print(f"  strukturerade: {len(res.to_update)}")
    for u in res.to_update:
        print(f"    {u['date']} {str(u['sport']):8} '{u['title']}' "
              f"→ {u['code']} ({u['source']}, {len(u['steps'])} steg)")
    if res.unmatched:
        print(f"  OMATCHADE (lämnade orörda, kräver coach-beslut): {len(res.unmatched)}")
        for u in res.unmatched:
            print(f"    {u['date']} {str(u['sport']):8} '{u['title']}'")
    print(f"  hoppade: {len(res.skipped)}")
    if not args.apply:
        print("  (dry-run — kör med --apply för att skriva)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
