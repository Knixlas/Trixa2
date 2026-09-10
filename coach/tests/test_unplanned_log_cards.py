"""Loggade pass utan planerad session ska synas (fynd från Nils via Sarah, 2026-09-10).

"Man kan logga ett pass på ett datum utan planerad session, och det
försvinner tyst ur gränssnittet." Loggen fanns i training_log och räknades
i volymen, men veckovyn byggdes bara ur planned_sessions — och en vecka
helt utan plan returnerade None. Nu får varje loggrad som inget planerat
pass tog som sitt utfört ett eget kort, "Oplanerat".
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C  # noqa: E402

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def _week(store):
    import trixa_api.ui as ui

    iso = MONDAY.isocalendar()
    return ui._fetch_current_week_data(_C(store), "81b667bc", iso[0], iso[1], TODAY, UID)


def _log(day, sport="Löpning", dur=40, title="Kvällsrunda"):
    return {"user_id": UID, "date": day.isoformat(), "sport": sport, "title": title,
            "duration_min": dur, "source": "manual"}


def test_logg_utan_plan_far_eget_kort():
    if TODAY.weekday() == 0:
        import pytest
        pytest.skip("måndag: ingen passerad dag i veckan")
    yesterday = TODAY - timedelta(days=1)
    week = _week({
        "planned_sessions": [{"id": "p1", "user_id": UID, "date": TODAY.isoformat(),
                              "sport": "Cykel", "title": "Rull", "status": "planned",
                              "origin": "nils", "duration_min": 30}],
        "training_log": [_log(yesterday)], "exercise_logs": [],
    })
    cards = [w for w in week["workouts"] if w.get("is_unplanned")]
    assert len(cards) == 1
    card = cards[0]
    assert card["date"] == yesterday.isoformat() and card["sport"] == "run"
    assert card["status"]["key"] == "done" and card["title"] == "Kvällsrunda"
    assert card["status"]["actual"]["duration_min"] == 40
    assert week["session_count"] == 2                   # planerat + oplanerat


def test_vecka_med_bara_loggar_ar_inte_tom():
    if TODAY.weekday() == 0:
        import pytest
        pytest.skip("måndag")
    week = _week({"planned_sessions": [], "training_log": [_log(TODAY - timedelta(days=1))],
                  "exercise_logs": []})
    assert week is not None
    assert week["plan_source"] == "log"
    assert len(week["workouts"]) == 1 and week["workouts"][0]["is_unplanned"]


def test_forbrukad_logg_dubbleras_inte():
    if TODAY.weekday() == 0:
        import pytest
        pytest.skip("måndag")
    yesterday = TODAY - timedelta(days=1)
    week = _week({
        "planned_sessions": [{"id": "p1", "user_id": UID, "date": yesterday.isoformat(),
                              "sport": "Löpning", "title": "Lugn", "status": "planned",
                              "origin": "nils", "duration_min": 40}],
        "training_log": [_log(yesterday)], "exercise_logs": [],
    })
    assert len(week["workouts"]) == 1
    assert week["workouts"][0]["status"]["key"] == "done"
    assert not week["workouts"][0].get("is_unplanned")


def test_andra_passet_samma_dag_far_eget_kort():
    if TODAY.weekday() == 0:
        import pytest
        pytest.skip("måndag")
    yesterday = TODAY - timedelta(days=1)
    week = _week({
        "planned_sessions": [{"id": "p1", "user_id": UID, "date": yesterday.isoformat(),
                              "sport": "Löpning", "title": "Lugn", "status": "planned",
                              "origin": "nils", "duration_min": 40}],
        "training_log": [_log(yesterday), _log(yesterday, sport="Sim", dur=45, title="Bad")],
        "exercise_logs": [],
    })
    titles = [w["title"] for w in week["workouts"]]
    assert titles == ["Lugn", "Bad"]                      # planerat först, sedan oplanerat
    assert week["workouts"][1]["is_unplanned"] and week["workouts"][1]["sport"] == "swim"
