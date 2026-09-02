"""Planerar-fynden ur kodöversynen 2026-09-02 (docs/12, avsnitt B).

B1  Regenerering av en påbörjad vecka skrev över genomförda pass.
B2  Misslyckad plan-skrivning sväljdes; cron loggade "Klar".
B3  "Nils vinner"-grinden stängdes tyst av vid databasfel.
B4  Cron-slotten "söndag exakt kl 20" hoppades över när pollen drev.
B5  Cron genererade veckor åt adepter som inte onboardat.
B6  Overtraining-override nådde aldrig plan_adjustment; volym-override
    kastade viloveckans skalning.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import pytest  # noqa: E402

from coach.tests.test_agent_api import UID, _C  # noqa: E402
from coach.trixa import cron, planner  # noqa: E402

TODAY = date.today()
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def _sw(d: date, sport="bike", title="Pass"):
    return SimpleNamespace(
        date=d, sport=sport, title=title, category="AE", duration_minutes=60,
        intensity="Z2", workout_data={"main_set": []}, notes="", details_markdown="",
        code="AE2_bike_01",
    )


def _plan(*workouts):
    return SimpleNamespace(week_start=MONDAY, workouts=list(workouts))


# ---------- B1: genomförda och passerade rader rörs inte ----------


def test_genomford_rad_skrivs_inte_over_vid_regenerering():
    st = {"planned_sessions": [
        {"id": "done-1", "user_id": UID, "date": MONDAY.isoformat(), "sport": "Cykel",
         "title": "Det jag faktiskt gjorde", "origin": "trixa2", "status": "completed"},
    ]}
    fake = _C(st)
    result = planner._persist_to_planned_sessions(
        fake, _plan(_sw(MONDAY, title="Något annat")), UID
    )
    row = st["planned_sessions"][0]
    assert row["title"] == "Det jag faktiskt gjorde"
    assert row["status"] == "completed"
    assert result["kept"] == 1 and result["updated"] == 0
    assert result["cancelled"] == 0          # den behållna raden cancel:as inte heller


def test_passerad_dag_ror_inte_ens_utan_completed_status():
    """Sync kan ligga efter — en passerad dag är historik oavsett status."""
    if TODAY.weekday() == 0:
        pytest.skip("måndag: ingen passerad dag i veckan att testa")
    st = {"planned_sessions": [
        {"id": "past-1", "user_id": UID, "date": MONDAY.isoformat(), "sport": "Cykel",
         "title": "Måndagens pass", "origin": "trixa2", "status": "planned"},
    ]}
    fake = _C(st)
    planner._persist_to_planned_sessions(fake, _plan(_sw(MONDAY, title="Nytt")), UID)
    assert st["planned_sessions"][0]["title"] == "Måndagens pass"


def test_kommande_dag_uppdateras_som_forut():
    future = MONDAY + timedelta(days=6)
    st = {"planned_sessions": [
        {"id": "fut-1", "user_id": UID, "date": future.isoformat(), "sport": "Cykel",
         "title": "Gammalt", "origin": "trixa2", "status": "planned"},
    ]}
    fake = _C(st)
    result = planner._persist_to_planned_sessions(
        fake, _plan(_sw(future, title="Nytt")), UID
    )
    assert st["planned_sessions"][0]["title"] == "Nytt"
    assert result["updated"] == 1


# ---------- B3: grinden får inte tystna ----------


def test_nils_grinden_kastar_i_stallet_for_att_tystna():
    class Boom:
        def table(self, *_):
            raise RuntimeError("postgrest nere")

    with pytest.raises(RuntimeError):
        planner._human_planned_sessions(Boom(), UID, MONDAY)


# ---------- B4/B5: cron ----------


def _sunday(hour: int, minute: int = 0) -> datetime:
    d = datetime(2026, 9, 6, hour, minute, tzinfo=timezone.utc)   # en söndag
    assert d.weekday() == 6
    return d


def test_slotten_ar_fran_kl_20_inte_exakt_kl_20():
    assert cron._should_run_now(_sunday(20, 5), None)
    assert cron._should_run_now(_sunday(21, 2), None)      # pollen drev förbi 20
    assert not cron._should_run_now(_sunday(19, 59), None)
    assert not cron._should_run_now(_sunday(21, 2), _sunday(20, 5))   # redan kört


def test_sover_till_nasta_hela_timme():
    secs = cron._seconds_to_next_hour(datetime(2026, 9, 6, 19, 57, 30, tzinfo=timezone.utc))
    assert 150 <= secs <= 160


def test_bara_onboardade_adepter_far_vecka(monkeypatch):
    fake = _C({"athlete_profiles": [
        {"user_id": "a", "onboarded_at": "2026-08-25T10:00:00Z"},
        {"user_id": "b", "onboarded_at": None},
    ]})
    monkeypatch.setattr(cron, "get_postgrest", lambda: fake)
    assert [a["user_id"] for a in cron._all_athletes()] == ["a"]


# ---------- B6: overrides som faktiskt når planen ----------


def _decisions():
    return {
        "phase_recommendation": {"phase": "build", "period": "build_1"},
        "discipline_hours": {"bike": 4.0, "run": 2.0},
        "overtraining": {"level": "low", "label": "Normal", "flag_count": 0, "flags": []},
        "plan_adjustment": None,
        "recovery_week": {"active": True, "volume_factor": 0.6},
    }


def test_overtraining_override_raknar_om_plan_adjustment():
    ov = {"scope": "overtraining", "override_decision": {"level": "moderate"},
          "motivation": "deltoid"}
    out, honored = planner._apply_overrides(_decisions(), [ov])
    assert honored == [ov]
    assert out["overtraining"]["level"] == "moderate"
    adj = out["plan_adjustment"]
    assert adj and adj["level"] == "moderate"
    assert adj["volume_reduction_pct"] > 0


def test_volym_override_behaller_viloveckans_skalning():
    ov = {"scope": "volume", "override_decision": {"weekly_hours": 10}, "motivation": "x"}
    out, _ = planner._apply_overrides(_decisions(), [ov])
    assert abs(sum(out["discipline_hours"].values()) - 6.0) < 0.3   # 10 h × 0,6
    assert out["volume_override"]["scaled_by"] == 0.6
