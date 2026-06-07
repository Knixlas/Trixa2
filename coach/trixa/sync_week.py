"""Full kedja: strukturera Nils fritext-pass → idempotent push till TrainingPeaks.

    python -m coach.trixa.sync_week [--week-start YYYY-MM-DD] [--user ID] [--apply]

Steg 1 (structure_week): fyll workout_code + steps på fritext-rader via den
deterministiska regeltabellen (coach/data/session_mapping.yaml). Omatchade rader
lämnas orörda och rapporteras.

Steg 2 (sync_planned_week_to_tp): idempotent push — replace-by-id +
skip-if-unchanged, så kedjan kan köras dagligen utan dubbletter.

Utan ``--apply``: dry-run (inga DB-skrivningar, inga TP-skrivningar). Default-vecka
är innevarande veckas måndag. Detta är den worker-vänliga entrypointen för
"Nils skriver fritext → strukturera → når klockan".
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

DEFAULT_USER_ID = "09db449d-b8fd-409a-b475-3401b0de9858"


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Strukturera + idempotent push till TP.")
    ap.add_argument("--user", default=DEFAULT_USER_ID)
    ap.add_argument("--week-start", default=None,
                    help="YYYY-MM-DD (default: innevarande veckas måndag)")
    ap.add_argument("--apply", action="store_true",
                    help="skriv steps till planned_sessions och pusha till TP")
    args = ap.parse_args(argv)

    from coach.engine.loader import load_workouts
    from coach.integrations.trainingpeaks.auth_store import supabase_cookie_provider
    from coach.integrations.trainingpeaks.client import TPClient
    from coach.integrations.trainingpeaks.workout_writer import sync_planned_week_to_tp
    from coach.trixa.db import get_postgrest
    from coach.trixa.planner import _build_athlete_profile_for_zones, _fetch_athlete
    from coach.trixa.structure_sessions import structure_week

    week_start = (
        date.fromisoformat(args.week_start) if args.week_start
        else _monday_of(date.today())
    )
    pool = {w["code"]: w for w in load_workouts()}
    pg = get_postgrest()
    prof = _build_athlete_profile_for_zones(_fetch_athlete(pg, args.user))
    mode = "APPLY" if args.apply else "DRY-RUN"
    week_end = week_start + timedelta(days=6)
    print(f"[{mode}] sync_week {week_start} → {week_end}  (user {args.user})")

    # Steg 1: strukturera fritext-rader.
    sres = structure_week(pg, args.user, week_start, pool, apply=args.apply)
    print(f"  steg 1 — strukturering: {len(sres.to_update)} fyllda, "
          f"{len(sres.unmatched)} omatchade, {len(sres.skipped)} hoppade")
    for u in sres.to_update:
        print(f"    + {u['date']} {str(u['sport']):8} '{u['title']}' → {u['code']} ({u['source']})")
    for u in sres.unmatched:
        print(f"    ? {u['date']} {str(u['sport']):8} '{u['title']}' — OMATCHAD (kräver coach-beslut)")

    # Steg 2: idempotent push.
    client = TPClient(cookie_provider=supabase_cookie_provider(args.user)) if args.apply else None
    results = sync_planned_week_to_tp(
        client, pg, args.user, week_start,
        css_sec_per_100m=prof.css_sec_per_100m,
        threshold_pace_sec_per_km=prof.threshold_pace_sec_per_km,
        dry_run=not args.apply,
    )
    if client is not None:
        client.close()
    from collections import Counter
    actions = Counter(r.action for r in results)
    print(f"  steg 2 — push (idempotent): {dict(actions)}")
    for r in results:
        wid = f" id={r.workout_id}" if r.workout_id else ""
        print(f"    {r.day} {str(r.sport):6} {r.action}{wid}")
        for w in r.warnings:
            print(f"      warn: {w}")
    if not args.apply:
        print("  (dry-run — kör med --apply för att skriva + pusha)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
