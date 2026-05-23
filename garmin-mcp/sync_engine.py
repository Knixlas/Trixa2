"""
Sync-motor: hämtar data från Garmin Connect och skriver till `garmin_coach`-schemat i Supabase.

Idempotent – körs säkert om utan duplicates tack vare upserts på naturliga nycklar
(`garmin_activity_id` för pass, `(athlete_id, metric_date)` för daily_metrics).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any

from supabase import Client

from garmin_client import GarminClient

logger = logging.getLogger("garmin-mcp.sync")

SCHEMA = "garmin_coach"

# Garmin's interna träningsstatus-koder → våra strängvärden
_TRAINING_STATUS_CODES = {
    0: "no_status",
    1: "recovery",
    2: "unproductive",
    3: "maintaining",
    4: "productive",
    5: "peaking",
    6: "overreaching",
    7: "detraining",
}

# Normalisera Garmins många activity-types till våra grupper
_ACTIVITY_TYPE_MAP = {
    "running": "running",
    "track_running": "running",
    "trail_running": "running",
    "treadmill_running": "running",
    "indoor_running": "running",
    "street_running": "running",
    "virtual_running": "running",
    "cycling": "cycling",
    "road_biking": "cycling",
    "mountain_biking": "cycling",
    "gravel_cycling": "cycling",
    "indoor_cycling": "cycling",
    "virtual_ride": "cycling",
    "cyclocross": "cycling",
    "lap_swimming": "swimming",
    "open_water_swimming": "open_water_swimming",
    "multi_sport": "multi_sport",
    "transition": "multi_sport",
    "strength_training": "strength",
}


def _normalize_activity_type(garmin_type_key: str | None) -> str:
    if not garmin_type_key:
        return "other"
    key = garmin_type_key.lower()
    if key in _ACTIVITY_TYPE_MAP:
        return _ACTIVITY_TYPE_MAP[key]
    # Heuristiska fallbacks
    if "running" in key:
        return "running"
    if "cycling" in key or "biking" in key or "ride" in key:
        return "cycling"
    if "swimming" in key:
        return "swimming"
    return "other"


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Transformers – Garmin response → tabellrad
# ---------------------------------------------------------------------------
def _activity_to_row(activity: dict, athlete_id: str) -> dict:
    """Mappar en Garmin-aktivitet till en rad i garmin_coach.activities."""
    type_key = (activity.get("activityType") or {}).get("typeKey")
    return {
        "athlete_id": athlete_id,
        "garmin_activity_id": activity.get("activityId"),
        "activity_name": activity.get("activityName"),
        "activity_type": _normalize_activity_type(type_key),
        "activity_sub_type": type_key,
        "start_time": activity.get("startTimeGMT"),
        "start_time_local": activity.get("startTimeLocal"),
        "timezone": activity.get("timeZoneId"),
        "duration_sec": _safe_int(activity.get("duration")),
        "moving_time_sec": _safe_int(activity.get("movingDuration")),
        "distance_m": _safe_float(activity.get("distance")),
        "elevation_gain_m": _safe_float(activity.get("elevationGain")),
        "elevation_loss_m": _safe_float(activity.get("elevationLoss")),
        "avg_hr": _safe_int(activity.get("averageHR")),
        "max_hr": _safe_int(activity.get("maxHR")),
        "avg_power": _safe_int(activity.get("averagePower")),
        "max_power": _safe_int(activity.get("maxPower")),
        "normalized_power": _safe_int(activity.get("normPower")),
        "avg_pace_sec_per_km": (
            round(1000.0 / activity["averageSpeed"], 2)
            if activity.get("averageSpeed") else None
        ),
        "avg_speed_mps": _safe_float(activity.get("averageSpeed")),
        "avg_cadence": _safe_int(
            activity.get("averageRunningCadenceInStepsPerMinute")
            or activity.get("averageBikingCadenceInRevPerMinute")
        ),
        "calories": _safe_int(activity.get("calories")),
        "training_effect_aerobic": _safe_float(activity.get("aerobicTrainingEffect")),
        "training_effect_anaerobic": _safe_float(activity.get("anaerobicTrainingEffect")),
        "training_load": _safe_float(activity.get("activityTrainingLoad")),
        "raw_data": activity,
    }


def _extract_training_status(raw: dict | None) -> tuple[str | None, dict | None]:
    """Plockar ut träningsstatus och tillhörande load-info ur Garmins response."""
    if not raw:
        return None, None
    
    status_str = None
    most_recent = raw.get("mostRecentTrainingStatus") or {}
    latest = most_recent.get("latestTrainingStatusData") or {}
    if isinstance(latest, dict):
        for v in latest.values():
            if isinstance(v, dict) and "trainingStatus" in v:
                status_str = _TRAINING_STATUS_CODES.get(v["trainingStatus"], "no_status")
                break
    
    load_info: dict | None = None
    balance = raw.get("mostRecentTrainingLoadBalance") or {}
    balance_map = balance.get("metricsTrainingLoadBalanceDTOMap") or {}
    if isinstance(balance_map, dict):
        for v in balance_map.values():
            if isinstance(v, dict):
                load_info = v
                break
    return status_str, load_info


def _daily_metrics_row(
    athlete_id: str,
    metric_date: date,
    readiness: list | dict | None,
    hrv: dict | None,
    sleep: dict | None,
    training_status: dict | None,
    max_metrics: list | dict | None,
    user_summary: dict | None,
) -> dict:
    """Mappar de samlade daglig-endpoints till en rad i garmin_coach.daily_metrics."""
    # Readiness kan vara lista eller dict beroende på version
    r = readiness[0] if isinstance(readiness, list) and readiness else (readiness or {})
    
    # HRV
    hrv_summary = (hrv or {}).get("hrvSummary") or {}
    hrv_baseline = hrv_summary.get("baseline") or {}
    hrv_status_raw = hrv_summary.get("status")
    hrv_status = hrv_status_raw.lower() if isinstance(hrv_status_raw, str) else None
    
    # Sleep
    sleep_dto = (sleep or {}).get("dailySleepDTO") or {}
    sleep_scores = (sleep_dto.get("sleepScores") or {}).get("overall") or {}
    
    # Träningsstatus
    status_str, load_info = _extract_training_status(training_status)
    
    # VO2max
    vo2_run = None
    vo2_cycle = None
    if max_metrics:
        mm = max_metrics[0] if isinstance(max_metrics, list) else max_metrics
        if isinstance(mm, dict):
            generic = mm.get("generic") or {}
            cycling = mm.get("cycling") or {}
            vo2_run = generic.get("vo2MaxValue")
            vo2_cycle = cycling.get("vo2MaxValue")
    
    us = user_summary or {}
    
    acute = (load_info or {}).get("acuteTrainingLoad") if load_info else None
    chronic = (load_info or {}).get("chronicTrainingLoad") if load_info else None
    
    return {
        "athlete_id": athlete_id,
        "metric_date": metric_date.isoformat(),
        # HRV
        "hrv_status": hrv_status if hrv_status in {"balanced","unbalanced","low","poor","no_status"} else None,
        "hrv_last_night_ms": _safe_float(hrv_summary.get("lastNightAvg")),
        "hrv_weekly_avg_ms": _safe_float(hrv_summary.get("weeklyAvg")),
        "hrv_baseline_low": _safe_float(hrv_baseline.get("lowUpper")),
        "hrv_baseline_high": _safe_float(hrv_baseline.get("balancedUpper")),
        # Sömn
        "sleep_duration_sec": _safe_int(sleep_dto.get("sleepTimeSeconds")),
        "sleep_deep_sec": _safe_int(sleep_dto.get("deepSleepSeconds")),
        "sleep_rem_sec": _safe_int(sleep_dto.get("remSleepSeconds")),
        "sleep_light_sec": _safe_int(sleep_dto.get("lightSleepSeconds")),
        "sleep_awake_sec": _safe_int(sleep_dto.get("awakeSleepSeconds")),
        "sleep_score": _safe_int(sleep_scores.get("value")),
        # Beredskap
        "readiness_score": _safe_int(r.get("score")),
        "readiness_level": (r.get("level") or "").lower() or None,
        # Body battery & stress
        "body_battery_high": _safe_int(us.get("bodyBatteryHighestValue")),
        "body_battery_low": _safe_int(us.get("bodyBatteryLowestValue")),
        "stress_avg": _safe_int(us.get("averageStressLevel")),
        # HR
        "resting_hr": _safe_int(us.get("restingHeartRate")),
        # VO2max
        "vo2max_running": _safe_float(vo2_run),
        "vo2max_cycling": _safe_float(vo2_cycle),
        # Träningsstatus
        "training_status": status_str,
        "acute_load": _safe_float(acute),
        "chronic_load": _safe_float(chronic),
        "load_ratio": (
            round(acute / chronic, 3)
            if acute and chronic and chronic > 0 else None
        ),
        "recovery_time_hours": _safe_int(r.get("recoveryTime")),
        "raw_data": {
            "readiness": r,
            "hrv": hrv,
            "sleep": sleep,
            "training_status": training_status,
            "user_summary": user_summary,
        },
    }


# ---------------------------------------------------------------------------
# SyncEngine
# ---------------------------------------------------------------------------
class SyncEngine:
    """Orkestrerar sync från Garmin → garmin_coach-schemat."""
    
    def __init__(self, garmin: GarminClient, supabase: Client, user_id: str | None = None):
        self.garmin = garmin
        self.sb = supabase
        self.user_id = user_id or None
        self._athlete_id: str | None = None
    
    def _table(self, name: str):
        return self.sb.schema(SCHEMA).table(name)
    
    # ------------------------------------------------------------------
    def ensure_athlete(self) -> str:
        """Hämta eller skapa atletprofilen. Returnerar athlete_id."""
        if self._athlete_id:
            return self._athlete_id
        
        query = self._table("athlete_profile").select("id")
        if self.user_id:
            query = query.eq("user_id", self.user_id)
        else:
            query = query.is_("user_id", "null")
        res = query.limit(1).execute()
        
        if res.data:
            self._athlete_id = res.data[0]["id"]
            logger.info("Befintlig athlete_profile: %s", self._athlete_id)
            return self._athlete_id
        
        payload: dict[str, Any] = {}
        if self.user_id:
            payload["user_id"] = self.user_id
        res = self._table("athlete_profile").insert(payload).execute()
        self._athlete_id = res.data[0]["id"]
        logger.info("Skapade athlete_profile: %s", self._athlete_id)
        return self._athlete_id
    
    # ------------------------------------------------------------------
    def _log_start(self, sync_type: str, **fields) -> tuple[str, int]:
        started_ms = int(time.time() * 1000)
        payload = {
            "athlete_id": self._athlete_id,
            "sync_type": sync_type,
            "status": "running",
            **fields,
        }
        res = self._table("sync_log").insert(payload).execute()
        return res.data[0]["id"], started_ms
    
    def _log_finish(
        self,
        log_id: str,
        status: str,
        records: int,
        started_ms: int,
        error: str | None = None,
    ) -> None:
        self._table("sync_log").update({
            "status": status,
            "records_synced": records,
            "error_message": error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": int(time.time() * 1000) - started_ms,
        }).eq("id", log_id).execute()
    
    # ------------------------------------------------------------------
    def sync_profile(self) -> int:
        """Uppdaterar athlete_profile med data från Garmin."""
        self.ensure_athlete()
        log_id, t0 = self._log_start("profile")
        try:
            today = date.today().isoformat()
            profile = self.garmin.api.get_user_profile() or {}
            hr_zones = self.garmin.api.get_heart_rates(today)
            max_metrics = self.garmin.api.get_max_metrics(today)
            
            vo2_run = vo2_cycle = None
            if max_metrics:
                mm = max_metrics[0] if isinstance(max_metrics, list) else max_metrics
                if isinstance(mm, dict):
                    vo2_run = (mm.get("generic") or {}).get("vo2MaxValue")
                    vo2_cycle = (mm.get("cycling") or {}).get("vo2MaxValue")
            
            update = {
                "garmin_user_id": str(profile.get("id")) if profile.get("id") else None,
                "display_name": profile.get("displayName") or profile.get("fullName"),
                "vo2max_running": _safe_float(vo2_run),
                "vo2max_cycling": _safe_float(vo2_cycle),
                "hr_zones": hr_zones,
                "raw_profile": profile,
            }
            update = {k: v for k, v in update.items() if v is not None}
            
            self._table("athlete_profile").update(update).eq("id", self._athlete_id).execute()
            self._log_finish(log_id, "success", 1, t0)
            logger.info("Profil synkad")
            return 1
        except Exception as e:
            logger.exception("sync_profile fallerade")
            self._log_finish(log_id, "failed", 0, t0, str(e))
            raise
    
    # ------------------------------------------------------------------
    def sync_activities(self, limit: int = 50) -> int:
        """Hämtar senaste `limit` aktiviteterna och upsertar dem."""
        athlete_id = self.ensure_athlete()
        log_id, t0 = self._log_start("activities", metadata={"limit": limit})
        try:
            activities = self.garmin.api.get_activities(0, limit) or []
            if not activities:
                self._log_finish(log_id, "success", 0, t0)
                return 0
            
            rows = [_activity_to_row(a, athlete_id) for a in activities]
            # Filtrera bort aktiviteter utan id (defensivt)
            rows = [r for r in rows if r["garmin_activity_id"] is not None]
            
            (self._table("activities")
                .upsert(rows, on_conflict="athlete_id,garmin_activity_id")
                .execute())
            
            self._log_finish(log_id, "success", len(rows), t0)
            logger.info("Synkade %d aktiviteter", len(rows))
            return len(rows)
        except Exception as e:
            logger.exception("sync_activities fallerade")
            self._log_finish(log_id, "failed", 0, t0, str(e))
            raise
    
    # ------------------------------------------------------------------
    def sync_daily(self, target_date: date) -> int:
        """Hämtar och upsertar daily_metrics för ett datum."""
        athlete_id = self.ensure_athlete()
        d = target_date.isoformat()
        log_id, t0 = self._log_start("daily_metrics", target_date=d)
        
        # Vi tål att enskilda endpoints fallerar – data kan saknas
        def _try(fn, *args):
            try:
                return fn(*args)
            except Exception as e:  # noqa: BLE001
                logger.warning("  ! %s misslyckades: %s", fn.__name__, e)
                return None
        
        try:
            readiness = _try(self.garmin.api.get_training_readiness, d)
            hrv = _try(self.garmin.api.get_hrv_data, d)
            sleep = _try(self.garmin.api.get_sleep_data, d)
            training_status = _try(self.garmin.api.get_training_status, d)
            max_metrics = _try(self.garmin.api.get_max_metrics, d)
            user_summary = _try(self.garmin.api.get_user_summary, d)
            
            row = _daily_metrics_row(
                athlete_id, target_date,
                readiness, hrv, sleep, training_status, max_metrics, user_summary,
            )
            (self._table("daily_metrics")
                .upsert(row, on_conflict="athlete_id,metric_date")
                .execute())
            
            self._log_finish(log_id, "success", 1, t0)
            logger.info("Synkade daily_metrics för %s", d)
            return 1
        except Exception as e:
            logger.exception("sync_daily fallerade för %s", d)
            self._log_finish(log_id, "failed", 0, t0, str(e))
            raise
    
    # ------------------------------------------------------------------
    def sync_daily_range(self, start: date, end: date) -> int:
        """Synka daily_metrics för ett datumintervall (inklusive båda ändpunkter)."""
        from datetime import timedelta
        total = 0
        d = start
        while d <= end:
            try:
                total += self.sync_daily(d)
            except Exception:
                logger.error("Hoppar över %s pga fel, fortsätter", d)
            d += timedelta(days=1)
        return total
