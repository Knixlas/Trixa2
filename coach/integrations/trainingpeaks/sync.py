"""TrainingPeaks → Supabase-sync.

Ersätter `garmin-mcp/sync_engine.py` som *producent* av de tabeller engine
läser (`garmin_coach.activities`, `garmin_coach.daily_metrics`). Engine-koden
rörs inte — samma schema, ny källa (se docs/06 §5-6).

Modulen är uppdelad i **rena transformfunktioner** (testbara utan nät) och en
tunn orkestrering som hämtar via TPClient och skriver via postgrest.

Mappning TP → daily_metrics-kolumner:
- metric `pulse` (type 5)      → resting_hr
- metric `hrv` (type 60)       → hrv_last_night_ms
- metric `sleep` timmar (6)    → sleep_score (PROXY, se nedan)
- PMC atl                      → acute_load
- PMC ctl                      → chronic_load
- PMC atl/ctl                  → load_ratio   (var NULL under Garmin)
- hrv_baseline_low/high        → BERÄKNAS (rullande fönster; TP saknar baseline)

`readiness_score`/`stress_avg` lämnas None (Garmin-proprietärt, korsar troligen
inte AutoSync) → engine degraderar konservativt. Verifiera fältformat mot
livedata; TP:s GET-shape är defensivt hanterad.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .client import TPClient

# TP metric type-ids (bekräftade i MCP metrics.py)
TYPE_PULSE = 5
TYPE_SLEEP_HOURS = 6
TYPE_HRV = 60


# ---------- rena transformer ----------


def _metric_value(day_record: dict, type_id: int) -> float | None:
    """Plocka ett metric-värde via type-id ur en dagspost.

    Defensiv mot TP:s GET-shape: letar i `details`-listan (samma form som
    POST-payloaden) och faller tillbaka på platta nycklar.
    """
    details = day_record.get("details")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, dict) and d.get("type") == type_id and d.get("value") is not None:
                try:
                    return float(d["value"])
                except (TypeError, ValueError):
                    return None
    return None


def _record_date(day_record: dict) -> str | None:
    raw = day_record.get("timeStamp") or day_record.get("date") or day_record.get("metricDate")
    if not raw:
        return None
    return str(raw)[:10]


def sleep_hours_to_score(hours: float | None) -> int | None:
    """Proxy för Garmins 0-100 sleep score ur sömntimmar.

    TP får sömntimmar via AutoSync, inte Garmins score. Linjär proxy mot 8 h.
    Ersätts om Garmins faktiska score visar sig korsa under annan type-id.
    """
    if hours is None:
        return None
    return max(0, min(100, round(hours / 8.0 * 100)))


def metrics_to_daily_rows(tp_days: list[dict], athlete_id: str) -> list[dict]:
    """TP consolidated metrics → daily_metrics-rader (utan baseline/load ännu)."""
    rows: list[dict] = []
    for rec in tp_days:
        d = _record_date(rec)
        if not d:
            continue
        sleep_h = _metric_value(rec, TYPE_SLEEP_HOURS)
        rows.append({
            "athlete_id": athlete_id,
            "metric_date": d,
            "resting_hr": _to_int(_metric_value(rec, TYPE_PULSE)),
            "hrv_last_night_ms": _metric_value(rec, TYPE_HRV),
            "sleep_score": sleep_hours_to_score(sleep_h),
            "readiness_score": None,   # Garmin-proprietärt, korsar ej
            "stress_avg": None,
        })
    rows.sort(key=lambda r: r["metric_date"])
    return rows


def add_hrv_baselines(rows: list[dict], window: int = 60) -> None:
    """Beräkna hrv_baseline_low/high + hrv_weekly_avg_ms in-place.

    baseline = rullande medel ± 1 SD över trailing `window` dagar (TP saknar
    Garmins baseline). weekly_avg = trailing 7-dagars medel. Rader antas
    sorterade stigande på metric_date.
    """
    hist: list[float] = []
    week: list[float] = []
    for r in rows:
        hrv = r.get("hrv_last_night_ms")
        # baseline från historik FÖRE denna dag (undvik läckage)
        if len(hist) >= 7:
            pool = hist[-window:]
            mean = statistics.mean(pool)
            sd = statistics.pstdev(pool) if len(pool) > 1 else 0.0
            r["hrv_baseline_low"] = round(mean - sd, 1)
            r["hrv_baseline_high"] = round(mean + sd, 1)
        else:
            r["hrv_baseline_low"] = None
            r["hrv_baseline_high"] = None
        if week:
            r["hrv_weekly_avg_ms"] = round(statistics.mean(week[-7:]), 1)
        else:
            r["hrv_weekly_avg_ms"] = None
        if hrv is not None:
            hist.append(hrv)
            week.append(hrv)


def pmc_to_load_by_date(pmc_rows: list[dict]) -> dict[str, dict]:
    """PMC (CTL/ATL/TSB) → {date: {acute_load, chronic_load, load_ratio}}."""
    out: dict[str, dict] = {}
    for e in pmc_rows:
        raw_day = e.get("workoutDay") or e.get("date")
        if not raw_day:
            continue
        d = str(raw_day)[:10]
        ctl = e.get("ctl")
        atl = e.get("atl")
        load_ratio = None
        if ctl not in (None, 0) and atl is not None:
            load_ratio = round(float(atl) / float(ctl), 3)
        out[d] = {
            "acute_load": round(float(atl), 1) if atl is not None else None,
            "chronic_load": round(float(ctl), 1) if ctl is not None else None,
            "load_ratio": load_ratio,
        }
    return out


def merge_load_into_daily(rows: list[dict], load_by_date: dict[str, dict]) -> None:
    """Lägg in acute/chronic/load_ratio i daily-rader (in-place)."""
    for r in rows:
        load = load_by_date.get(r["metric_date"])
        if load:
            r.update(load)
        else:
            r.setdefault("acute_load", None)
            r.setdefault("chronic_load", None)
            r.setdefault("load_ratio", None)


# completed-aktiviteter → activities-rader

# TP workoutTypeValueId → engine activity_type (samma id-rymd som SPORT_TYPE_MAP).
# Fältnamnen nedan är verifierade mot TP:s v6-svar mot livedata 2026-06-07.
_TYPE_BY_VALUE_ID = {1: "swimming", 2: "cycling", 3: "running", 4: "multi_sport",
                     5: "other", 8: "cycling", 9: "strength", 12: "rowing", 13: "other"}


def workouts_to_activity_rows(tp_workouts: list[dict], athlete_id: str) -> list[dict]:
    """Genomförda TP-pass → activities-rader (engine läser duration_sec+start_time).

    Verifierat mot TP:s v6-svar (livedata):
    - faktisk tid = `totalTime` i **timmar** (`totalTimeActual` finns inte);
      `completed` är alltid None → "genomfört" = totalTime > 0 (rena planerade
      pass har totalTime None/0).
    - sporttyp = `workoutTypeValueId` (inte `workoutTypeFamilyId`, som saknas här).
    - starttid = `startTime` (faktisk), fallback `workoutDay` (midnatt).

    `garmin_activity_id` är **bigint** med UNIQUE (athlete_id, garmin_activity_id);
    TP:s numeriska workoutId återanvänds direkt som unik nyckel.
    """
    rows: list[dict] = []
    for w in tp_workouts:
        actual_h = w.get("totalTime")          # faktisk tid i timmar; None/0 = bara planerat
        if not actual_h:
            continue
        wid = w.get("workoutId") or w.get("id")
        day = w.get("startTime") or w.get("workoutDay")
        if wid is None or not day:
            continue
        try:
            activity_id = int(wid)             # bigint-kolumn — TP workoutId är numeriskt
        except (TypeError, ValueError):
            continue
        rows.append({
            "athlete_id": athlete_id,
            "garmin_activity_id": activity_id,
            "start_time": str(day),
            "duration_sec": int(round(float(actual_h) * 3600)),  # TP timmar → sek
            "activity_type": _TYPE_BY_VALUE_ID.get(w.get("workoutTypeValueId"), "other"),
        })
    return rows


def _to_int(v: float | None) -> int | None:
    return int(round(v)) if v is not None else None


# ---------- TP utfört → training_log (MASTER) ----------

# TP workoutTypeValueId → training_log.sport. Matchar legacy-MAJORITETEN i
# training_log (svenska namn) så TP-rader är konsekventa med strava-raderna.
_TL_SPORT_BY_VALUE_ID = {1: "Sim", 2: "Cykel", 3: "Lopning", 4: "Brick",
                         5: "Crosstrain", 8: "Cykel", 9: "Styrka", 12: "Rodd",
                         13: "Promenad"}

# Normalisering för dedup: folda alla stavningar/språk → kanonisk disciplin.
_SPORT_CANON = {
    "run": "run", "running": "run", "löpning": "run", "lopning": "run", "virtualrun": "run",
    "bike": "bike", "biking": "bike", "cycling": "bike", "ride": "bike",
    "cykel": "bike", "cykling": "bike", "mtb": "bike",
    "swim": "swim", "swimming": "swim", "sim": "swim", "simning": "swim",
    "strength": "strength", "styrka": "strength",
}


def canon_sport(s: str | None) -> str:
    """Folda en sport-sträng till kanonisk disciplin (run/bike/swim/strength/…)."""
    key = (s or "").strip().lower()
    return _SPORT_CANON.get(key, key)


def tp_workout_to_training_log_row(w: dict, user_id: str) -> dict | None:
    """Ett genomfört TP-pass → en training_log-rad (source='tp'). None om ej genomfört."""
    actual_h = w.get("totalTime")          # faktisk tid i timmar; None/0 = bara planerat
    if not actual_h:
        return None
    wid = w.get("workoutId") or w.get("id")
    day = w.get("startTime") or w.get("workoutDay")
    if wid is None or not day:
        return None
    try:
        tp_id = int(wid)
    except (TypeError, ValueError):
        return None

    row: dict[str, Any] = {
        "user_id": user_id,
        "tp_workout_id": tp_id,
        "date": str(day)[:10],
        "sport": _TL_SPORT_BY_VALUE_ID.get(w.get("workoutTypeValueId"), "other"),
        "title": w.get("title"),
        "duration_min": round(float(actual_h) * 60.0, 1),
        "source": "tp",
    }
    dist = w.get("distance")
    if dist:
        row["distance_km"] = round(float(dist) / 1000.0, 2)
    for tp_key, tl_key in (("heartRateAverage", "avg_hr"), ("heartRateMaximum", "max_hr"),
                           ("powerAverage", "avg_power"), ("normalizedPowerActual", "normalized_power")):
        v = w.get(tp_key)
        if v is not None:
            row[tl_key] = int(round(float(v)))
    tss = w.get("tssActual")
    if tss is not None:
        row["tss"] = float(tss)
    return row


def dedup_training_log_rows(tp_rows: list[dict], existing_non_tp: list[dict]) -> tuple[list[dict], list[dict]]:
    """Filtrera bort TP-rader som redan finns som icke-TP-pass (t.ex. strava).

    Matchning: samma user, samma datum, kanonisk sport, varaktighet inom ±10 %
    (minst ±2 min). Skyddar mot dubbelräkning. Returnerar (fresh, skipped).
    """
    fresh: list[dict] = []
    skipped: list[dict] = []
    for r in tp_rows:
        c = canon_sport(r.get("sport"))
        d = r["date"]
        dur = r.get("duration_min") or 0
        tol = max(2.0, 0.10 * dur)
        is_dup = any(
            str(e.get("date"))[:10] == d
            and canon_sport(e.get("sport")) == c
            and e.get("duration_min") is not None
            and abs(float(e["duration_min"]) - dur) <= tol
            for e in existing_non_tp
        )
        (skipped if is_dup else fresh).append(r)
    return fresh, skipped


# ---------- orkestrering ----------


@dataclass
class SyncResult:
    sync_type: str
    records: int = 0
    status: str = "success"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _schema_table(pg: Any, table: str):
    """garmin_coach.<table> via postgrest. Faller tillbaka på from_ om schema()
    saknas i klientversionen."""
    if hasattr(pg, "schema"):
        return pg.schema("garmin_coach").from_(table)
    return pg.from_(table)  # kräver att default-schemat är satt på klienten


def sync_daily(
    client: TPClient,
    athlete_id: str,
    start: date,
    end: date,
    pg: Any = None,
    baseline_lookback_days: int = 60,
) -> SyncResult:
    """Hämta hälsometrik + PMC för intervallet och skriv daily_metrics."""
    try:
        # hämta extra historik bakåt för stabil HRV-baseline
        from datetime import timedelta
        fetch_start = start - timedelta(days=baseline_lookback_days)
        metric_days = client.get_metrics(fetch_start, end)
        pmc = client.get_performance_data(fetch_start, end)

        rows = metrics_to_daily_rows(metric_days, athlete_id)
        add_hrv_baselines(rows, window=baseline_lookback_days)
        merge_load_into_daily(rows, pmc_to_load_by_date(pmc))

        # skriv bara raderna inom det begärda fönstret
        write_rows = [r for r in rows if start.isoformat() <= r["metric_date"] <= end.isoformat()]
        if pg is not None and write_rows:
            _schema_table(pg, "daily_metrics").upsert(
                write_rows, on_conflict="athlete_id,metric_date"
            ).execute()
        return SyncResult("daily", records=len(write_rows))
    except Exception as e:  # noqa: BLE001 — logga och rapportera, krascha inte cron
        return SyncResult("daily", status="failed", error=str(e))


def sync_activities(
    client: TPClient,
    athlete_id: str,
    start: date,
    end: date,
    pg: Any = None,
) -> SyncResult:
    """Hämta genomförda pass och skriv activities."""
    try:
        workouts = client.get_workouts(start, end)
        rows = workouts_to_activity_rows(workouts, athlete_id)
        if pg is not None and rows:
            _schema_table(pg, "activities").upsert(
                rows, on_conflict="athlete_id,garmin_activity_id"
            ).execute()
        return SyncResult("activities", records=len(rows))
    except Exception as e:  # noqa: BLE001
        return SyncResult("activities", status="failed", error=str(e))


def sync_completed_to_training_log(
    client: TPClient,
    user_id: str,
    start: date,
    end: date,
    pg: Any = None,
    dry_run: bool = False,
) -> SyncResult:
    """TP genomförda pass → public.training_log (MASTER utfört).

    Dedup mot befintliga icke-TP-rader (strava etc.) på user+datum+sport+varaktighet
    så vi aldrig dubbelräknar. Idempotent upsert på (user_id, tp_workout_id).
    `pg` används för läsning även i dry_run; `dry_run` hoppar bara skrivningen.
    """
    try:
        workouts = client.get_workouts(start, end)
        tp_rows = [r for r in (tp_workout_to_training_log_row(w, user_id) for w in workouts) if r]

        existing_non_tp: list[dict] = []
        if pg is not None:
            res = (
                pg.from_("training_log")
                .select("date,sport,duration_min,source")
                .eq("user_id", user_id)
                .gte("date", start.isoformat())
                .lte("date", end.isoformat())
                .execute()
            )
            existing_non_tp = [e for e in (getattr(res, "data", None) or []) if e.get("source") != "tp"]

        fresh, skipped = dedup_training_log_rows(tp_rows, existing_non_tp)

        if not dry_run and pg is not None and fresh:
            pg.from_("training_log").upsert(fresh, on_conflict="user_id,tp_workout_id").execute()

        warnings = []
        if skipped:
            warnings.append(f"{len(skipped)} TP-pass hoppade (dubblett mot befintlig icke-TP-rad)")
        return SyncResult("training_log", records=len(fresh), warnings=warnings)
    except Exception as e:  # noqa: BLE001
        return SyncResult("training_log", status="failed", error=str(e))
