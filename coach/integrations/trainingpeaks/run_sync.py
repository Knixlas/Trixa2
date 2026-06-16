"""CLI för TP→Supabase-sync (Railway-worker / cron).

    python -m coach.integrations.trainingpeaks.run_sync --days 2

Ersätter `garmin-mcp`-cronens roll: matar `garmin_coach.activities` +
`daily_metrics` från TrainingPeaks i stället för Garmin. Auth via Supabase-
backad cookie **per användare** (``--user``). Se docs/07_TP_SYNC_RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from .auth_store import supabase_cookie_provider
from .client import TPClient
from .sync import sync_activities, sync_completed_to_training_log, sync_daily

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
    started_at = datetime.now(timezone.utc)

    pg = None
    if not args.dry_run:
        from coach.trixa.db import get_postgrest
        pg = get_postgrest()

    client = TPClient(cookie_provider=supabase_cookie_provider(args.user, pg))
    end = date.today()
    start = end - timedelta(days=args.days)

    daily = sync_daily(client, args.athlete_id, start, end, pg=pg)
    acts = sync_activities(client, args.athlete_id, start, end, pg=pg)
    completed = sync_completed_to_training_log(
        client, args.user, start, end, pg=pg, dry_run=args.dry_run
    )

    for r in (daily, acts, completed):
        line = f"[{r.sync_type}] {r.status} records={r.records}"
        if r.error:
            line += f" error={r.error}"
        print(line)
        for w in r.warnings:
            print(f"  warn: {w}")

    results = (daily, acts, completed)
    success = all(r.status == "success" for r in results)
    if pg is not None:
        try:
            pg.table("integration_runs").insert({
                "user_id": args.user,
                "integration": "trainingpeaks",
                "operation": "read_sync",
                "status": "success" if success else "failed",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "records_processed": sum(r.records for r in results),
                "error_message": "; ".join(
                    f"{r.sync_type}: {r.error}" for r in results if r.error
                ) or None,
                "metadata": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "results": {
                        r.sync_type: {
                            "status": r.status,
                            "records": r.records,
                            "warnings": r.warnings,
                        }
                        for r in results
                    },
                },
            }).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"[integration_runs] warn: kunde inte logga synkstatus: {exc}")
    client.close()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
