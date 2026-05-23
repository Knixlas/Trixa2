"""
Garmin MCP Server
==================
En Model Context Protocol-server som ger Claude (eller annan MCP-klient) 
tillgång till data från Garmin Connect via det inofficiella python-garminconnect-biblioteket.

Tänkt som grund för en kodbaserad triathloncoach.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from garmin_client import GarminClient

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("garmin-mcp")

mcp = FastMCP("Garmin Coach")
client = GarminClient(
    email=os.getenv("GARMIN_EMAIL"),
    password=os.getenv("GARMIN_PASSWORD"),
    token_dir=Path(os.getenv("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser(),
)


def _today_iso() -> str:
    return date.today().isoformat()


def _serialize(obj: Any) -> str:
    """Säker JSON-serialisering – datum -> ISO-strängar."""
    def default(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(f"Type {type(o)} not serializable")
    return json.dumps(obj, default=default, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Aktiviteter
# ---------------------------------------------------------------------------
@mcp.tool()
def list_activities(limit: int = 10, activity_type: str | None = None) -> str:
    """
    Lista de senaste aktiviteterna från Garmin Connect.

    Args:
        limit: Max antal aktiviteter att hämta (default 10).
        activity_type: Filtrera på typ: 'running', 'cycling', 'swimming', 
                       'open_water_swimming', 'multi_sport', etc. None = alla.
    """
    activities = client.api.get_activities(0, limit)
    if activity_type:
        activities = [
            a for a in activities
            if activity_type.lower() in a.get("activityType", {}).get("typeKey", "").lower()
        ]
    
    # Plocka ut det viktigaste för coach-kontext
    slim = [{
        "activityId": a.get("activityId"),
        "name": a.get("activityName"),
        "type": a.get("activityType", {}).get("typeKey"),
        "startTime": a.get("startTimeLocal"),
        "duration_min": round((a.get("duration") or 0) / 60, 1),
        "distance_km": round((a.get("distance") or 0) / 1000, 2),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": a.get("calories"),
        "training_effect_aerobic": a.get("aerobicTrainingEffect"),
        "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
        "training_load": a.get("activityTrainingLoad"),
    } for a in activities]
    return _serialize(slim)


@mcp.tool()
def get_activity_details(activity_id: int) -> str:
    """
    Hämta detaljerad information om en specifik aktivitet, inklusive 
    varv/splits, zoner och underliggande mätvärden.

    Args:
        activity_id: ID från list_activities.
    """
    details = client.api.get_activity(activity_id)
    splits = client.api.get_activity_splits(activity_id)
    hr_zones = client.api.get_activity_hr_in_timezones(activity_id)
    return _serialize({
        "details": details,
        "splits": splits,
        "hr_zones": hr_zones,
    })


@mcp.tool()
def get_weekly_summary(weeks_back: int = 0) -> str:
    """
    Sammanställning för en träningsvecka (mån–sön): totalt antal pass,
    sammanlagd tid och distans per sport, samt total träningsbelastning.

    Args:
        weeks_back: 0 = denna vecka, 1 = förra veckan, osv.
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday() + 7 * weeks_back)
    sunday = monday + timedelta(days=6)
    
    # Hämta tillräckligt många aktiviteter och filtrera
    activities = client.api.get_activities(0, 50)
    week_activities = [
        a for a in activities
        if monday <= datetime.fromisoformat(a["startTimeLocal"]).date() <= sunday
    ]
    
    by_sport: dict[str, dict[str, float]] = {}
    total_load = 0.0
    for a in week_activities:
        sport = a.get("activityType", {}).get("typeKey", "unknown")
        bucket = by_sport.setdefault(sport, {"count": 0, "duration_min": 0.0, "distance_km": 0.0})
        bucket["count"] += 1
        bucket["duration_min"] += (a.get("duration") or 0) / 60
        bucket["distance_km"] += (a.get("distance") or 0) / 1000
        total_load += a.get("activityTrainingLoad") or 0

    return _serialize({
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "total_activities": len(week_activities),
        "total_training_load": round(total_load, 1),
        "by_sport": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by_sport.items()},
    })


# ---------------------------------------------------------------------------
# Träningsstatus & återhämtning
# ---------------------------------------------------------------------------
@mcp.tool()
def get_training_status(target_date: str | None = None) -> str:
    """
    Aktuell träningsstatus från Garmin: productive, maintaining, peaking, 
    overreaching, unproductive, detraining, recovery, eller no status.
    
    Args:
        target_date: ISO-datum (YYYY-MM-DD). Default = idag.
    """
    d = target_date or _today_iso()
    return _serialize(client.api.get_training_status(d))


@mcp.tool()
def get_training_readiness(target_date: str | None = None) -> str:
    """
    Träningsberedskap för idag (0–100), inkl. underliggande faktorer som 
    sömn, HRV-status, återhämtningstid och akut belastning.

    Args:
        target_date: ISO-datum. Default = idag.
    """
    d = target_date or _today_iso()
    return _serialize(client.api.get_training_readiness(d))


@mcp.tool()
def get_hrv_status(target_date: str | None = None) -> str:
    """
    Heart Rate Variability-data för en natt: status (balanced/unbalanced/low/poor),
    weekly average och baseline-intervall.

    Args:
        target_date: ISO-datum. Default = idag.
    """
    d = target_date or _today_iso()
    return _serialize(client.api.get_hrv_data(d))


@mcp.tool()
def get_sleep(target_date: str | None = None) -> str:
    """
    Sömndata: total sömn, faser (djup/REM/lätt), sömnpoäng, andning.

    Args:
        target_date: ISO-datum. Default = idag.
    """
    d = target_date or _today_iso()
    return _serialize(client.api.get_sleep_data(d))


@mcp.tool()
def get_vo2max(target_date: str | None = None) -> str:
    """
    VO2max för löpning och cykling, från senaste uppdaterade värdet.

    Args:
        target_date: ISO-datum. Default = idag.
    """
    d = target_date or _today_iso()
    return _serialize(client.api.get_max_metrics(d))


# ---------------------------------------------------------------------------
# Profil & zoner
# ---------------------------------------------------------------------------
@mcp.tool()
def get_user_profile() -> str:
    """
    Användarprofil: ålder, vikt, vilo-HR, max-HR och tröskelvärden.
    Bas för all zon-beräkning.
    """
    return _serialize({
        "profile": client.api.get_user_profile(),
        "summary": client.api.get_user_summary(_today_iso()),
    })


@mcp.tool()
def get_heart_rate_zones() -> str:
    """
    Användarens HR-zoner per sport (löpning, cykling, simning, generell).
    """
    return _serialize(client.api.get_heart_rates(_today_iso()))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Startar Garmin MCP-server via stdio…")
    mcp.run(transport="stdio")
