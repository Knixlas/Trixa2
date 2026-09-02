"""generate_week från början till slut mot fejkad databas.

Torrkörningen 2026-09-02 föll på en saknad import (SimpleNamespace) som 337
gröna tester inte såg — ingen av dem körde generate_week hela vägen. Det
här testet gör det, med dry_run=False mot fejken, så att både passvalet,
schemaläggningen, renderingen och alla fem skrivningarna i _persist_week
faktiskt exekveras.
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


def _store(today: date) -> dict:
    return {
        "athlete_profiles": [{
            "id": "81b667bc", "user_id": UID, "goal": "ironman", "weekly_hours": 8,
            "sports": ["swim", "bike", "run", "strength"], "recovery_week_ratio": "2:1",
            "ftp": 250, "lthr": 165, "lthr_bike": 160, "max_hr": 185, "resting_hr": 48,
            "swim_css": "2:00", "run_threshold_pace": "5:00",
            "race_date": (today + timedelta(weeks=20)).isoformat(),
            "phase_state": {}, "onboarded_at": "2026-08-01T00:00:00Z",
            "preferred_rest_days": ["monday"], "equipment": {}, "preferred_settings": {},
            "active_concerns": [],
        }],
        "races": [{"athlete_id": "81b667bc", "date": (today + timedelta(weeks=20)).isoformat(),
                   "priority": "A", "name": "Test-IM", "distance": "ironman"}],
        "coach_overrides": [], "weekly_reports": [], "planned_sessions": [],
        "training_log": [], "daily_metrics": [], "coach_alerts": [],
    }


def test_generate_week_hela_vagen_med_skrivning(monkeypatch):
    from coach.trixa import planner

    today = date(2026, 9, 2)
    monday = date(2026, 9, 7)
    st = _store(today)
    fake = _C(st)
    monkeypatch.setattr(planner, "get_supabase", lambda: fake)
    monkeypatch.setenv("TRIXA_PUSH_TO_TP", "0")

    plan = planner.generate_week(UID, monday, dry_run=False, today=today)

    assert plan.phase in ("prep", "base", "build", "peak")
    assert plan.workouts, "inga pass valdes"
    assert all(sw.details_markdown or sw.sport == "rest" for sw in plan.workouts)
    persist = plan.engine_decisions["planned_sessions_persist"]
    assert persist["written"] == len(st["planned_sessions"]) > 0
    assert st["athlete_profiles"][0]["phase_state"]["last_planned_week_start"] == monday.isoformat()
    assert "category_decisions" in plan.engine_decisions
    assert plan.engine_decisions["strength_protocol_detail"]["reason"]


def test_generate_week_dry_run_skriver_inget(monkeypatch):
    from coach.trixa import planner

    today = date(2026, 9, 2)
    st = _store(today)
    monkeypatch.setattr(planner, "get_supabase", lambda: _C(st))
    plan = planner.generate_week(UID, date(2026, 9, 7), dry_run=True, today=today)
    assert plan.workouts
    assert st["planned_sessions"] == []
    assert st["athlete_profiles"][0]["phase_state"] == {}
