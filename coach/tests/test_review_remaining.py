"""De sista fynden ur kodöversynen 2026-09-02 (docs/12): H4, I9, B4-syskon, G5.

H4/I9  Dashboarden läste training_log tre gånger, planned_sessions fyra,
       exercise_logs sex, races två. Nu en prefetch per tabell som vyerna
       skivar — och samma resultat som per-fråga-vägen.
B4     Cron höll last_run i RAM; en omstart på söndagskvällen kunde ge en
       andra körning. Nu läses phase_state.last_planned_week_start.
G5     TP-synken skrev readiness/stress som None (nollade andra källor) och
       satte HRV-baseline efter 7 sampel.
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


class _Counting(_C):
    """Räknar execute()-anrop per tabell."""

    def __init__(self, st):
        super().__init__(st)
        self.calls: dict[str, int] = {}

    def table(self, n):
        q = super().table(n)
        real = q.execute

        def execute():
            self.calls[n] = self.calls.get(n, 0) + 1
            return real()

        q.execute = execute
        return q


def _store():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    last_wed = monday - timedelta(days=5)
    return {
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID, "onboarded_at": "x",
                              "weekly_hours": 8, "race_date": (today + timedelta(days=90)).isoformat()}],
        "planned_sessions": [
            {"id": "p1", "user_id": UID, "date": last_wed.isoformat(), "sport": "Löpning",
             "title": "Lugn", "workout_code": "", "duration_min": 40, "status": "planned",
             "origin": "trixa2"},
            {"id": "p2", "user_id": UID, "date": (monday + timedelta(days=2)).isoformat(),
             "sport": "Styrka", "title": "Ben", "workout_code": "", "duration_min": 45,
             "status": "planned", "origin": "nils", "exercises": [{"name": "Knäböj", "reps": 5}]},
        ],
        "training_log": [
            {"user_id": UID, "date": last_wed.isoformat(), "sport": "Löpning",
             "duration_min": 42, "source": "tp"},
        ],
        "exercise_logs": [
            {"user_id": UID, "session_date": (monday - timedelta(days=7)).isoformat(),
             "exercise_name": "Knäböj", "sets": 3, "reps": 5, "weight_from": 60.0, "effort": 2},
        ],
        "races": [{"athlete_id": "81b667bc", "date": (today + timedelta(days=90)).isoformat(),
                   "priority": "A", "name": "Testloppet"}],
    }


def test_prefetch_ger_samma_resultat_som_per_fraga():
    import trixa_api.ui as ui

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    a, b = _Counting(_store()), _Counting(_store())
    pre = ui._prefetch_dashboard(a, UID, "81b667bc", monday, today)
    assert set(pre) >= {"log", "planned", "exercise_logs", "races"}

    iso = monday.isocalendar()
    with_pre = ui._fetch_current_week_data(a, "81b667bc", iso[0], iso[1], today, UID, pre)
    without = ui._fetch_current_week_data(b, "81b667bc", iso[0], iso[1], today, UID)
    assert [w["title"] for w in with_pre["workouts"]] == [w["title"] for w in without["workouts"]]
    assert with_pre["workouts"][0]["exercises_to_log"][0]["weight_from"] == \
        without["workouts"][0]["exercises_to_log"][0]["weight_from"]

    comp_pre = ui._compliance_by_week(a, "81b667bc", today, UID, pre)
    comp_raw = ui._compliance_by_week(b, "81b667bc", today, UID)
    assert comp_pre == comp_raw and comp_pre

    ctx_pre = ui._build_season_context(a, _store()["athlete_profiles"][0], today, monday, pre)
    ctx_raw = ui._build_season_context(b, _store()["athlete_profiles"][0], today, monday)
    assert ctx_pre["race_label"] == ctx_raw["race_label"] == "Testloppet"
    assert ctx_pre["readiness"] == ctx_raw["readiness"]


def test_prefetch_sparar_databasanrop():
    import trixa_api.ui as ui

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    a, b = _Counting(_store()), _Counting(_store())
    athlete = _store()["athlete_profiles"][0]

    pre = ui._prefetch_dashboard(a, UID, "81b667bc", monday, today)
    iso = monday.isocalendar()
    ui._fetch_current_week_data(a, "81b667bc", iso[0], iso[1], today, UID, pre)
    ui._build_season_context(a, athlete, today, monday, pre)
    ui._fetch_current_week_data(b, "81b667bc", iso[0], iso[1], today, UID)
    ui._build_season_context(b, athlete, today, monday)

    assert sum(a.calls.values()) <= 4                       # en per tabell
    assert sum(b.calls.values()) > sum(a.calls.values()) + 4


def test_cron_hoppar_redan_planerad_vecka():
    from coach.trixa import cron

    monday = date(2026, 9, 7)
    assert cron._already_planned({"phase_state": {"last_planned_week_start": "2026-09-07"}}, monday)
    assert not cron._already_planned({"phase_state": {"last_planned_week_start": "2026-08-31"}}, monday)
    assert not cron._already_planned({}, monday)


def test_hrv_baseline_kraver_tva_veckor():
    from coach.integrations.trainingpeaks import sync

    rows = [{"metric_date": f"2026-08-{d:02d}", "hrv_last_night_ms": 50 + (d % 3)} for d in range(1, 21)]
    sync.add_hrv_baselines(rows)
    assert rows[7]["hrv_baseline_low"] is None        # 7 sampel: för lite
    assert rows[14]["hrv_baseline_low"] is not None   # 14 sampel: baseline
