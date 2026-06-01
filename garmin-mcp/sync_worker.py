"""garmin-mcp/sync_worker.py — long-running worker som syncar Garmin -> Supabase.

Tänkt att köra som separat Railway-service. Schemalägger en daglig full
sync via apscheduler. EN inloggning per körning för att minimera Garmins
token-invalidering pga "suspekt aktivitet".

Skillnad mot GitHub Actions sync.yml:
- Stabil Railway-IP (Garmin behandlar GitHub Actions IPs hårdare)
- Persistent process — token-cachen i ~/.garminconnect överlever mellan
  körningar (inget cold-start från Secret varje gång)
- En process som triggar EN sync per dygn — ingen risk för cron-överlapp

Vid uppstart kör den en sync direkt (säkerställa att Supabase-storage har
fresh tokens). Sen schemalagt 06:30 UTC dagligen.

Kör lokalt:
    python sync_worker.py

Kör på Railway:
    Skapa ny service från samma repo, branch=main (eller trixa-app-skeleton),
    Root directory = garmin-mcp, Start command = python sync_worker.py.
    Env vars: GARMIN_EMAIL, GARMIN_PASSWORD, SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY (samma som GitHub Actions Secrets).

Se docs/05_GARMIN_WORKER_ON_RAILWAY.md för deploy-steg.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import date, timedelta
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from supabase import create_client

from garmin_client import GarminClient
from sync_engine import SyncEngine

logger = logging.getLogger("garmin-mcp.worker")


def _make_engine() -> SyncEngine:
    """Skapa en SyncEngine-instans (delar logik med sync.py)."""
    load_dotenv()
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "SUPABASE_URL och SUPABASE_SERVICE_ROLE_KEY maste finnas i env"
        )
    supabase = create_client(supabase_url, supabase_key)
    garmin = GarminClient(
        email=os.getenv("GARMIN_EMAIL"),
        password=os.getenv("GARMIN_PASSWORD"),
        token_dir=Path(os.getenv("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
        supabase_client=supabase,
    )
    user_id = os.getenv("SUPABASE_USER_ID") or None
    return SyncEngine(garmin=garmin, supabase=supabase, user_id=user_id)


def run_full_sync() -> None:
    """Kor en full sync (profile + activities + daily) i samma process.

    EN inloggning, EN token-rotation per kornning. Garmin behandlar detta
    som normal anvandning — flera separate login-anrop per dag triggar
    token-invalidering.
    """
    logger.info("=== Startar full sync ===")
    engine = None
    try:
        engine = _make_engine()
    except Exception as e:  # noqa: BLE001
        logger.exception("Kunde inte skapa engine: %s", e)
        return

    try:
        prof_rows = engine.sync_profile()
        logger.info("Profile: %s rad uppdaterad", prof_rows)

        act_rows = engine.sync_activities(limit=10)
        logger.info("Activities: %s synkade", act_rows)

        today = date.today()
        yesterday = today - timedelta(days=1)
        daily_rows = engine.sync_daily_range(yesterday, today)
        logger.info("Daily metrics: %s dagar synkade", daily_rows)
    except Exception as e:  # noqa: BLE001
        logger.exception("Sync failade: %s", e)
    finally:
        # Spara eventuellt refreshade tokens (single-use refresh protection)
        try:
            engine.garmin.save_tokens()
        except Exception:  # noqa: BLE001
            pass

    logger.info("=== Full sync klart ===")


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Sync-worker startar...")

    # Vid uppstart: kor en sync direkt sa Supabase oauth_tokens far
    # fresh tokens omedelbart. Om processen restartas pga deploy/crash
    # ar tokens da uppdaterade.
    run_full_sync()

    scheduler = BlockingScheduler(timezone="UTC")

    # Daglig sync 06:30 UTC (~08:30 svensk sommartid). Samma tid som
    # GitHub Actions cron — gor det enkelt att jamfora resultat under
    # overgangsperioden.
    scheduler.add_job(
        run_full_sync,
        CronTrigger(hour=6, minute=30),
        name="daily_garmin_sync",
        max_instances=1,  # ingen overlap om en korning drojer
        coalesce=True,    # om vi missar en window, kor bara en gang
    )

    logger.info("Schemalagt: full sync 06:30 UTC dagligen")

    def shutdown(sig: int, frame) -> None:  # noqa: ARG001
        logger.info("Signal %s — stanger worker...", sig)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
