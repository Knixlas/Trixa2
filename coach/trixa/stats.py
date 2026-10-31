"""Träningsstatistik: timmar, pass och distans per gren och period.

Läser MASTER ``training_log`` via ``fetch_clean_log`` — samma regel som
resten av appen: bara träningsgrenar, och samma fysiska pass räknas en
gång även när det finns från flera källor (TP + Strava + manuellt).

Ren beräkning (``compute_stats``) skild från läsningen (``fetch_all_log``)
så att siffrorna går att testa utan databas.
"""

from __future__ import annotations

from datetime import date, timedelta

from coach.trixa import sports
from coach.trixa.training_log import fetch_clean_log

# Visningsordning. Grenar utanför listan (om registret växer) hamnar sist.
SPORT_ORDER = ("swim", "bike", "run", "strength", "brick", "yoga")

TREND_WEEKS = 12

# Hur många tomma år i rad som får passera innan vi slutar leta bakåt.
_EMPTY_YEARS_STOP = 2
_EARLIEST_YEAR = 2000


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _empty() -> dict:
    return {"hours": 0.0, "sessions": 0, "km": 0.0, "tss": 0.0, "longest_min": 0.0}


def _add(bucket: dict, row: dict) -> None:
    dur = _f(row.get("duration_min"))
    bucket["hours"] += dur / 60.0
    bucket["sessions"] += 1
    bucket["km"] += _f(row.get("distance_km"))
    bucket["tss"] += _f(row.get("tss"))
    bucket["longest_min"] = max(bucket["longest_min"], dur)


def _round(bucket: dict) -> dict:
    return {
        "hours": round(bucket["hours"], 1),
        "sessions": bucket["sessions"],
        "km": round(bucket["km"], 1),
        "tss": round(bucket["tss"]),
        "longest_min": round(bucket["longest_min"]),
    }


def period_bounds(today: date) -> dict[str, tuple[date, date]]:
    """Perioderna sidan visar, som [start, slut] inklusive."""
    week_start = _monday(today)
    prev_week_start = week_start - timedelta(days=7)
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    return {
        "week": (week_start, today),
        "prev_week": (prev_week_start, week_start - timedelta(days=1)),
        "month": (month_start, today),
        "prev_month": (prev_month_end.replace(day=1), prev_month_end),
        "year": (today.replace(month=1, day=1), today),
        "total": (date(_EARLIEST_YEAR, 1, 1), today),
    }


def compute_stats(rows: list[dict], today: date) -> dict:
    """Aggregera städade loggrader.

    Returnerar ``periods[period]["total"|sport]`` med timmar/pass/km/TSS/
    längsta pass, en vecka-för-vecka-trend för de senaste ``TREND_WEEKS``
    veckorna, och vilka grenar som förekommer.
    """
    bounds = period_bounds(today)
    periods = {p: {"total": _empty()} for p in bounds}

    trend_start = _monday(today) - timedelta(weeks=TREND_WEEKS - 1)
    trend = {
        trend_start + timedelta(weeks=i): {"total": 0.0}
        for i in range(TREND_WEEKS)
    }

    first_date: date | None = None
    seen: set[str] = set()

    for row in rows:
        try:
            d = date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            continue
        if d > today:
            continue
        key = sports.canon(row.get("sport"), "other") or "other"
        if not sports.is_training(key):
            continue
        seen.add(key)
        first_date = d if first_date is None or d < first_date else first_date

        for p, (start, end) in bounds.items():
            if start <= d <= end:
                _add(periods[p]["total"], row)
                _add(periods[p].setdefault(key, _empty()), row)

        wk = _monday(d)
        if wk in trend:
            hrs = _f(row.get("duration_min")) / 60.0
            trend[wk]["total"] += hrs
            trend[wk][key] = trend[wk].get(key, 0.0) + hrs

    order = [s for s in SPORT_ORDER if s in seen] + sorted(seen - set(SPORT_ORDER))

    out_periods = {
        p: {k: _round(v) for k, v in buckets.items()} for p, buckets in periods.items()
    }

    trend_rows = [
        {
            "week_start": wk.isoformat(),
            "label": f"v{wk.isocalendar()[1]}",
            "total": round(v["total"], 1),
            "by_sport": {s: round(v.get(s, 0.0), 2) for s in order},
        }
        for wk, v in sorted(trend.items())
    ]
    max_week = max((t["total"] for t in trend_rows), default=0.0)
    avg_week = round(sum(t["total"] for t in trend_rows) / TREND_WEEKS, 1)

    total_weeks = 0
    if first_date:
        total_weeks = ((_monday(today) - _monday(first_date)).days // 7) + 1

    return {
        "periods": out_periods,
        "sports": order,
        "labels": {s: sports.sv(s) for s in order},
        "trend": trend_rows,
        "trend_max": max_week,
        "trend_avg": avg_week,
        "first_date": first_date.isoformat() if first_date else None,
        "total_avg_week": (
            round(out_periods["total"]["total"]["hours"] / total_weeks, 1) if total_weeks else 0.0
        ),
        "bounds": {p: (s.isoformat(), e.isoformat()) for p, (s, e) in bounds.items()},
    }


def fetch_all_log(client, user_id: str, today: date) -> list[dict]:
    """Hela loggen, städad. Läses ett kalenderår i taget bakåt så att ingen
    enskild fråga slår i PostgREST:s radtak, och stannar efter några tomma år."""
    rows: list[dict] = []
    empty = 0
    year = today.year
    while year >= _EARLIEST_YEAR and empty < _EMPTY_YEARS_STOP:
        end = today if year == today.year else date(year, 12, 31)
        chunk = fetch_clean_log(client, user_id, date(year, 1, 1).isoformat(), end.isoformat())
        if chunk:
            rows.extend(chunk)
            empty = 0
        else:
            empty += 1
        year -= 1
    return rows
