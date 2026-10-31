"""Statistiksidan: timmar per gren och period, dedup tvärs källor."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.trixa.stats import compute_stats, period_bounds  # noqa: E402

TODAY = date(2026, 10, 31)   # lördag, vecka 44


def _r(d, sport, mins, source="tp", km=None, **kw):
    return {"date": d, "sport": sport, "duration_min": mins, "source": source,
            "distance_km": km, **kw}


def test_perioder():
    b = period_bounds(TODAY)
    assert b["week"] == (date(2026, 10, 26), TODAY)
    assert b["prev_week"] == (date(2026, 10, 19), date(2026, 10, 25))
    assert b["month"] == (date(2026, 10, 1), TODAY)
    assert b["prev_month"] == (date(2026, 9, 1), date(2026, 9, 30))
    assert b["year"] == (date(2026, 1, 1), TODAY)


def test_timmar_per_gren_och_period():
    rows = [
        _r("2026-10-31", "Cykel", 90, km=30),     # vecka, månad, år
        _r("2026-10-27", "Sim", 60, km=2),        # vecka
        _r("2026-10-10", "Löpning", 30, km=5),    # månad
        _r("2026-03-01", "Löpning", 120, km=20),  # år
        _r("2025-06-01", "Sim", 60),              # bara totalt
    ]
    s = compute_stats(rows, TODAY)
    P = s["periods"]
    assert P["week"]["total"]["hours"] == 2.5
    assert P["week"]["bike"]["hours"] == 1.5
    assert P["week"]["swim"]["sessions"] == 1
    assert P["month"]["total"]["hours"] == 3.0
    assert P["year"]["run"]["hours"] == 2.5
    assert P["year"]["run"]["km"] == 25.0
    assert P["total"]["total"]["hours"] == 6.0
    assert P["total"]["total"]["sessions"] == 5
    assert P["total"]["run"]["longest_min"] == 120
    assert s["sports"] == ["swim", "bike", "run"]
    assert s["first_date"] == "2025-06-01"


def test_icke_traning_och_framtid_raknas_inte():
    rows = [
        _r("2026-10-30", "Promenad", 60),
        _r("2026-10-30", "Övrigt", 60),
        _r("2026-11-02", "Cykel", 60),   # framtid
        _r("2026-10-30", "Yoga", 30),
    ]
    s = compute_stats(rows, TODAY)
    assert s["periods"]["week"]["total"]["hours"] == 0.5
    assert s["sports"] == ["yoga"]


def test_trend_tolv_veckor_med_staplar_per_gren():
    rows = [_r("2026-10-27", "Sim", 60), _r("2026-10-28", "Cykel", 30),
            _r("2026-08-10", "Löpning", 45)]
    s = compute_stats(rows, TODAY)
    assert len(s["trend"]) == 12
    last = s["trend"][-1]
    assert last["label"] == "v44"
    assert last["total"] == 1.5
    assert last["by_sport"]["swim"] == 1.0
    assert s["trend_max"] == 1.5


def test_tom_logg():
    s = compute_stats([], TODAY)
    assert s["sports"] == []
    assert s["periods"]["total"]["total"]["hours"] == 0
    assert s["first_date"] is None


def test_statistiksidan_renderar_och_dedupar_kallor():
    from coach.tests.test_yoga_and_done_status import _ui_client
    from coach.tests.test_agent_api import UID
    import coach.trixa.clock as clock

    store = {"training_log": [
        {"user_id": UID, "date": "2026-10-27", "sport": "Sim", "duration_min": 37.1,
         "source": "tp", "planned_session_id": "ps-1"},
        {"user_id": UID, "date": "2026-10-27", "sport": "swim", "duration_min": 54,
         "source": "manual", "planned_session_id": "ps-1"},
        {"user_id": UID, "date": "2026-10-28", "sport": "Cykel", "duration_min": 60,
         "source": "tp"},
    ]}
    orig = clock.today
    clock.today = lambda: TODAY
    try:
        c, _ = _ui_client(store)
        r = c.get("/ui/stats")
    finally:
        clock.today = orig
    assert r.status_code == 200, r.text
    assert "Timmar per gren" in r.text
    assert "1,6" in r.text          # 37 + 60 min = 1,6 h — simpasset räknas en gång
    assert "Statistik" in r.text
