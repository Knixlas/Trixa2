"""Schemalagd planner-runner. Körs av Railway-worker eller GitHub Actions.

För Railway: definiera en separat "worker"-service med start-command
`python -m coach.trixa.cron`. Den loopar med 1-timmes intervall och
kör generate_week varje söndag 20:00 UTC.

Alternativt — kör som GitHub Actions schedule (`cron: '0 20 * * 0'`).
För enkelhet kör Railway-worker här.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

from coach.trixa.db import get_postgrest
from coach.trixa.planner import generate_week


logger = logging.getLogger("trixa.cron")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Kör vid denna timme (UTC). 20:00 UTC = sön 21:00 vintertid / 22:00 sommartid.
_RUN_HOUR_UTC = int(os.environ.get("TRIXA_CRON_HOUR_UTC", "20"))
_RUN_WEEKDAY = int(os.environ.get("TRIXA_CRON_WEEKDAY", "6"))  # 6 = söndag
_POLL_INTERVAL_SEC = int(os.environ.get("TRIXA_CRON_POLL_SEC", "3600"))  # 1h


def _next_monday(today: date) -> date:
    """Nästa måndag (även om idag är måndag, returnerar om 7 dagar framåt)."""
    days_to_monday = (7 - today.weekday()) % 7
    if days_to_monday == 0:
        days_to_monday = 7
    return today + timedelta(days=days_to_monday)


def _should_run_now(now_utc: datetime, last_run: datetime | None) -> bool:
    """Returnerar True om vi ska köra just nu."""
    if now_utc.weekday() != _RUN_WEEKDAY:
        return False
    if now_utc.hour != _RUN_HOUR_UTC:
        return False
    if last_run and (now_utc - last_run).total_seconds() < 23 * 3600:
        # Skydd: kör inte två gånger samma vecka
        return False
    return True


def _all_athletes() -> list[dict]:
    client = get_postgrest()
    res = client.table("athlete_profiles").select("user_id").execute()
    return res.data or []


def _run_once_for(athlete_user_id: str) -> None:
    next_mon = _next_monday(date.today())
    logger.info("Genererar vecka %s för adept %s", next_mon.isoformat(), athlete_user_id)
    try:
        plan = generate_week(
            athlete_user_id=athlete_user_id,
            week_start=next_mon,
            dry_run=False,
        )
        logger.info(
            "Klar: fas=%s, pass=%d, alerts=%d, week_id=%s",
            plan.phase,
            len(plan.workouts),
            plan.engine_decisions.get("alerts_written", 0),
            plan.engine_decisions.get("persisted_week_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Fel vid generering för %s: %s", athlete_user_id, exc)


def main() -> int:
    """Loopa och kör planner på schemaTime. Stoppar aldrig självmant."""
    logger.info("Trixa-cron startad. Schemavalt: vid %02d:00 UTC, weekday=%d. Poll var %ds.",
                _RUN_HOUR_UTC, _RUN_WEEKDAY, _POLL_INTERVAL_SEC)
    last_run: datetime | None = None
    while True:
        now = datetime.now(timezone.utc)
        if _should_run_now(now, last_run):
            athletes = _all_athletes()
            logger.info("Triggerar planner för %d adepter", len(athletes))
            for a in athletes:
                if a.get("user_id"):
                    _run_once_for(a["user_id"])
            last_run = now
        time.sleep(_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main() or 0)
