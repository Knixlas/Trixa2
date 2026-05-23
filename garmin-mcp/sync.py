"""
sync.py – CLI för att synka Garmin Connect-data till Supabase.

Exempel:
    python sync.py profile                       # Uppdatera atletprofil
    python sync.py activities --limit 50         # Senaste 50 passen
    python sync.py daily                         # Dagens metrics
    python sync.py daily --date 2025-11-15       # Specifikt datum
    python sync.py daily --from 2025-11-01 --to 2025-11-15   # Datumintervall
    python sync.py full --activities 100 --days 30  # Allt på en gång
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

from garmin_client import GarminClient
from sync_engine import SyncEngine


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _make_engine() -> SyncEngine:
    load_dotenv()
    
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        sys.exit("FEL: SUPABASE_URL och SUPABASE_SERVICE_ROLE_KEY måste finnas i .env")
    
    garmin = GarminClient(
        email=os.getenv("GARMIN_EMAIL"),
        password=os.getenv("GARMIN_PASSWORD"),
        token_dir=Path(os.getenv("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
    )
    supabase = create_client(supabase_url, supabase_key)
    user_id = os.getenv("SUPABASE_USER_ID") or None
    return SyncEngine(garmin=garmin, supabase=supabase, user_id=user_id)


def cmd_profile(args, engine: SyncEngine) -> int:
    return engine.sync_profile()


def cmd_activities(args, engine: SyncEngine) -> int:
    return engine.sync_activities(limit=args.limit)


def cmd_daily(args, engine: SyncEngine) -> int:
    if args.from_date and args.to_date:
        return engine.sync_daily_range(args.from_date, args.to_date)
    target = args.date or date.today()
    return engine.sync_daily(target)


def cmd_full(args, engine: SyncEngine) -> int:
    print("→ Profil"); engine.sync_profile()
    print(f"→ Senaste {args.activities} aktiviteter"); engine.sync_activities(limit=args.activities)
    start = date.today() - timedelta(days=args.days - 1)
    end = date.today()
    print(f"→ Daily metrics {start} → {end}")
    return engine.sync_daily_range(start, end)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    p = argparse.ArgumentParser(description="Synka Garmin Connect → Supabase (garmin_coach)")
    sub = p.add_subparsers(dest="cmd", required=True)
    
    sub.add_parser("profile", help="Uppdatera atletprofil")
    
    p_act = sub.add_parser("activities", help="Synka senaste aktiviteterna")
    p_act.add_argument("--limit", type=int, default=20)
    
    p_daily = sub.add_parser("daily", help="Synka daily metrics")
    p_daily.add_argument("--date", type=_parse_date, default=None, help="YYYY-MM-DD (default idag)")
    p_daily.add_argument("--from", dest="from_date", type=_parse_date, default=None)
    p_daily.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    
    p_full = sub.add_parser("full", help="Profil + aktiviteter + daily metrics")
    p_full.add_argument("--activities", type=int, default=50, help="Antal aktiviteter (default 50)")
    p_full.add_argument("--days", type=int, default=7, help="Antal dagar bakåt för daily (default 7)")
    
    args = p.parse_args()
    
    engine = _make_engine()
    
    handlers = {
        "profile": cmd_profile,
        "activities": cmd_activities,
        "daily": cmd_daily,
        "full": cmd_full,
    }
    
    try:
        result = handlers[args.cmd](args, engine)
        print(f"\n✅ Klart. Antal rader synkade: {result}")
        return 0
    except KeyboardInterrupt:
        print("\nAvbruten.")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"\n❌ Sync fallerade: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
