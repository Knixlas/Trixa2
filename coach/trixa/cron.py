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

# Daglig TP→Supabase läs-sync (recovery + aktiviteter). Gated av TRIXA_TP_SYNC
# tills go-live (kräver TP-cookie). Samma worker kör då både den dagliga
# läs-synken och den veckovisa planner-pushen — ingen ny Railway-service behövs.
_TP_SYNC_ENABLED = os.environ.get("TRIXA_TP_SYNC", "").lower() in ("1", "true", "yes")
_TP_SYNC_HOUR_UTC = int(os.environ.get("TRIXA_TP_SYNC_HOUR_UTC", "5"))  # ≈07 svensk sommartid
_TP_SYNC_DAYS = int(os.environ.get("TRIXA_TP_SYNC_DAYS", "2"))

# Daglig strukturering av Nils fritext-pass + idempotent TP-push (innevarande +
# nästa vecka). Fångar ad-hoc-redigeringar i planned_sessions så de når klockan
# utan manuell körning. Gated av TRIXA_PUSH_TO_TP (samma flagga som planner-pushen);
# idempotent → säker att köra dagligen (oförändrade pass hoppas). Körs efter
# läs-synken så ev. färsk recovery-data redan finns.
_PUSH_ENABLED = os.environ.get("TRIXA_PUSH_TO_TP", "").lower() in ("1", "true", "yes")
_PUSH_HOUR_UTC = int(os.environ.get("TRIXA_PUSH_HOUR_UTC", "6"))


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


def _run_tp_sync() -> None:
    """Daglig TP→Supabase-sync (recovery + aktiviteter). Best-effort — fel
    loggas men fäller aldrig worker-loopen."""
    try:
        from coach.integrations.trainingpeaks.run_sync import main as tp_sync_main
        rc = tp_sync_main(["--days", str(_TP_SYNC_DAYS)])
        logger.info("TP-sync klar (rc=%d)", rc)
    except Exception as exc:  # noqa: BLE001
        logger.error("TP-sync fel: %s", exc)


def _run_structure_and_push() -> None:
    """Daglig: strukturera Nils fritext-pass → steps + idempotent push till TP,
    för innevarande och nästa vecka, för alla adepter. Best-effort — fel loggas
    men fäller aldrig worker-loopen. Idempotent: oförändrade pass hoppas."""
    from collections import Counter

    try:
        from coach.engine.loader import load_workouts
        from coach.integrations.trainingpeaks.auth_store import supabase_cookie_provider
        from coach.integrations.trainingpeaks.client import TPClient
        from coach.integrations.trainingpeaks.workout_writer import sync_planned_week_to_tp
        from coach.trixa.planner import _build_athlete_profile_for_zones, _fetch_athlete
        from coach.trixa.structure_sessions import structure_week

        pg = get_postgrest()
        pool = {w["code"]: w for w in load_workouts()}
        monday = date.today() - timedelta(days=date.today().weekday())
        weeks = [monday, monday + timedelta(days=7)]
        client = TPClient(cookie_provider=supabase_cookie_provider())
        try:
            for a in _all_athletes():
                uid = a.get("user_id")
                if not uid:
                    continue
                try:
                    prof = _build_athlete_profile_for_zones(_fetch_athlete(pg, uid))
                except Exception as exc:  # noqa: BLE001
                    logger.error("Profil-fel %s: %s", uid, exc)
                    continue
                for ws in weeks:
                    try:
                        sres = structure_week(pg, uid, ws, pool, apply=True)
                        res = sync_planned_week_to_tp(
                            client, pg, uid, ws,
                            css_sec_per_100m=prof.css_sec_per_100m,
                            threshold_pace_sec_per_km=prof.threshold_pace_sec_per_km,
                            dry_run=False,
                        )
                        logger.info(
                            "structure+push %s %s: strukturerade=%d push=%s omatchade=%d",
                            uid, ws.isoformat(), len(sres.to_update),
                            dict(Counter(r.action for r in res)), len(sres.unmatched),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("structure+push fel %s %s: %s", uid, ws, exc)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("structure+push topp-fel: %s", exc)


def main() -> int:
    """Loopa och kör planner + (ev.) daglig TP-sync. Stoppar aldrig självmant."""
    logger.info(
        "Trixa-cron startad. Planner: %02d:00 UTC weekday=%d. TP läs-sync: %s (%02d:00). "
        "Strukturera+push: %s (%02d:00). Poll var %ds.",
        _RUN_HOUR_UTC, _RUN_WEEKDAY,
        "på" if _TP_SYNC_ENABLED else "av", _TP_SYNC_HOUR_UTC,
        "på" if _PUSH_ENABLED else "av", _PUSH_HOUR_UTC, _POLL_INTERVAL_SEC,
    )
    last_run: datetime | None = None
    last_tp_sync: date | None = None
    last_push: date | None = None
    while True:
        now = datetime.now(timezone.utc)
        if _should_run_now(now, last_run):
            athletes = _all_athletes()
            logger.info("Triggerar planner för %d adepter", len(athletes))
            for a in athletes:
                if a.get("user_id"):
                    _run_once_for(a["user_id"])
            last_run = now
        # Daglig TP läs-sync (gated). En gång per dygn vid TP-sync-timmen.
        if _TP_SYNC_ENABLED and now.hour == _TP_SYNC_HOUR_UTC and last_tp_sync != now.date():
            _run_tp_sync()
            last_tp_sync = now.date()
        # Daglig strukturering + idempotent push (gated). Fångar Nils ad-hoc-pass.
        if _PUSH_ENABLED and now.hour == _PUSH_HOUR_UTC and last_push != now.date():
            _run_structure_and_push()
            last_push = now.date()
        time.sleep(_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main() or 0)
