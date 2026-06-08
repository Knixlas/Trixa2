"""CLI: pusha en veckas planned_sessions → TrainingPeaks som strukturerade pass.

    python -m coach.integrations.trainingpeaks.push_week [--week-start YYYY-MM-DD] [--dry-run]

Läser MASTER planned_sessions för veckan och skapar strukturerade TP-pass
(→ TP→Garmin AutoSync → klockan). Default: nästa vecka (måndag). `--dry-run`
bygger payloads utan att skriva till TP. Se docs/06–07.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from .auth_store import supabase_cookie_provider
from .client import TPClient
from .workout_writer import sync_planned_week_to_tp

# Niklas (profiles.id). Multi-adept: skicka --user-id.
DEFAULT_USER_ID = "09db449d-b8fd-409a-b475-3401b0de9858"


def _next_monday(today: date) -> date:
    return today + timedelta(days=((7 - today.weekday()) % 7) or 7)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pusha planned_sessions → TP")
    ap.add_argument("--week-start", default=None, help="måndag YYYY-MM-DD (default: nästa måndag)")
    ap.add_argument("--user-id", default=DEFAULT_USER_ID)
    ap.add_argument("--dry-run", action="store_true", help="bygg payloads utan att skriva till TP")
    ap.add_argument("--out", default=None, help="skriv resultatet till denna fil (robust mot shell-redirect)")
    args = ap.parse_args(argv)

    lines: list[str] = []
    rc = 0
    try:
        from coach.trixa.db import get_postgrest
        from coach.trixa.planner import _build_athlete_profile_for_zones, _fetch_athlete

        pg = get_postgrest()
        week_start = date.fromisoformat(args.week_start) if args.week_start else _next_monday(date.today())
        prof = _build_athlete_profile_for_zones(_fetch_athlete(pg, args.user_id))
        client = None if args.dry_run else TPClient(cookie_provider=supabase_cookie_provider(args.user_id))

        results = sync_planned_week_to_tp(
            client, pg, args.user_id, week_start,
            css_sec_per_100m=prof.css_sec_per_100m,
            threshold_pace_sec_per_km=prof.threshold_pace_sec_per_km,
            dry_run=args.dry_run,
        )
        lines.append(f"Vecka {week_start} -> {len(results)} pass (idempotent):")
        for r in results:
            lines.append(f"  {r.day} {r.sport} {r.action} workout_id={r.workout_id}")
            for w in r.warnings:
                lines.append(f"    warn: {w}")
    except Exception as e:  # noqa: BLE001 — fånga så resultatfilen alltid skrivs
        lines.append(f"FEL: {type(e).__name__}: {e}")
        rc = 1

    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
