"""CLI för TP→Supabase-sync (Railway-worker / cron).

    python -m coach.integrations.trainingpeaks.run_sync --days 2

Ersätter `garmin-mcp`-cronens roll: matar `garmin_coach.activities` +
`daily_metrics` från TrainingPeaks i stället för Garmin. Auth via Supabase-
backad cookie **per användare** (``--user``). Se docs/07_TP_SYNC_RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from .auth_store import supabase_cookie_provider
from .client import TPClient
from .sync import sync_activities, sync_daily

# garmin_coach.athlete_profile.id (recovery-cachen nycklas på detta) — se CLAUDE.md
DEFAULT_ATHLETE_ID = "98057fa1-4fb9-48f5-be86-b31272dcfed0"
# user_id (public.profiles.id) för TP-cookien — skilt från athlete_id ovan.
# Multi-tenant: --user väljer vems cookie som läses ur public.tp_auth.
DEFAULT_USER_ID = "09db449d-b8fd-409a-b475-3401b0de9858"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TP→Supabase sync")
    ap.add_argument("--days", type=int, default=2, help="dagar bakåt att synka")
    ap.add_argument("--athlete-id", default=DEFAULT_ATHLETE_ID)
    ap.add_argument("--user", default=DEFAULT_USER_ID,
                    help="user_id för TP-cookien (multi-tenant)")
    ap.add_argument("--dry-run", action="store_true",
                    help="hämta + transformera men skriv inte till Supabase")
    args = ap.parse_args(argv)

    pg = None
    if not args.dry_run:
        from coach.trixa.db import get_postgrest
        pg = get_postgrest()

    client = TPClient(cookie_provider=supabase_cookie_provider(args.user, pg))
    end = date.today()
    start = end - timedelta(days=args.days)

    daily = sync_daily(client, args.athlete_id, start, end, pg=pg)
    acts = sync_activities(client, args.athlete_id, start, end, pg=pg)

    for r in (daily, acts):
        line = f"[{r.sync_type}] {r.status} records={r.records}"
        if r.error:
            line += f" error={r.error}"
        print(line)
        for w in r.warnings:
            print(f"  warn: {w}")

    return 0 if daily.status == "success" and acts.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
