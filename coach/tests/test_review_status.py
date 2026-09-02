"""Status/dedup/datum-fynden ur kodöversynen 2026-09-02 (docs/12, avsnitt F).

F1  Källdedupen slog ihop två riktiga pass från samma källa.
F2  date.today() var serverns (UTC) dag, inte adeptens.
F3  Cancelled-rader räknades som "Missad" och dubblade planerade timmar.
F4  MCP get_week skickade cancelled-rader som plan.
F5  Eget pass: rå insert mot unik-indexet → 500, loggen nåddes aldrig.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402
from coach.trixa import clock  # noqa: E402


# ---------- F1 ----------


def _row(source, dur, sport="Cykel", day="2026-09-01"):
    return {"date": day, "sport": sport, "duration_min": dur, "source": source}


def test_tva_pass_fran_samma_kalla_behalls_bada():
    import trixa_api.ui as ui

    kept = ui._dedup_log_rows([_row("tp", 25), _row("tp", 25)])
    assert len(kept) == 2                           # pendling dit och hem


def test_samma_pass_fran_tp_och_strava_blir_ett():
    import trixa_api.ui as ui

    kept = ui._dedup_log_rows([_row("strava", 60.2), _row("tp", 60.0)])
    assert len(kept) == 1 and kept[0]["source"] == "tp"


# ---------- F2 ----------


def test_klockan_ger_adeptens_dag(monkeypatch):
    monkeypatch.setenv("TRIXA_TZ", "Europe/Stockholm")
    # 2026-09-06 23:30 UTC = 2026-09-07 01:30 i Stockholm (sommartid).
    fake_now = datetime(2026, 9, 6, 23, 30, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now.astimezone(tz) if tz else fake_now

    monkeypatch.setattr(clock, "datetime", _DT)
    assert clock.today() == date(2026, 9, 7)


def test_inga_serverlokala_today_kvar():
    import re

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for rel in ("trixa_api", "coach/trixa"):
        for p in (root / rel).glob("*.py"):
            if p.name == "clock.py":          # dokumentationen nämner det gamla anropet
                continue
            src = p.read_text(encoding="utf-8")
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"\bdate_type\.today\(\)|(?<![\w.])date\.today\(\)", line):
                    offenders.append(f"{p.name}:{i}")
    assert offenders == [], offenders


# ---------- F3/F4 ----------


def test_cancelled_rader_raknas_inte_i_foljsamhet_eller_timmar():
    import trixa_api.ui as ui

    today = date.today()
    last_wed = today - timedelta(days=today.weekday() + 5)
    fake = _C({
        "planned_sessions": [
            {"user_id": UID, "date": last_wed.isoformat(), "sport": "Cykel", "title": "A",
             "workout_code": "", "duration_min": 60, "status": "cancelled"},
            {"user_id": UID, "date": last_wed.isoformat(), "sport": "Löpning", "title": "B",
             "workout_code": "", "duration_min": 30, "status": "planned"},
        ],
        "training_log": [{"user_id": UID, "date": last_wed.isoformat(), "sport": "Löpning",
                          "duration_min": 30, "source": "manual"}],
        "exercise_logs": [],
    })
    comp = ui._compliance_by_week(fake, "81b667bc", today, UID)
    iso = last_wed.isocalendar()
    assert comp[(iso[0], iso[1])] == "green"        # spökraden skulle gjort den röd
    hours = ui._planned_hours_by_week(fake, UID, last_wed, last_wed)
    assert abs(hours[(iso[0], iso[1])] - 0.5) < 0.01


def test_get_week_utelamnar_cancelled():
    st = {"api_tokens": [], "profiles": [{"id": UID}],
          "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
          "exercise_logs": [],
          "planned_sessions": [
              {"id": "a", "user_id": UID, "date": "2026-09-08", "sport": "Cykel",
               "title": "Borttagen", "status": "cancelled", "origin": "trixa2"},
              {"id": "b", "user_id": UID, "date": "2026-09-08", "sport": "Löpning",
               "title": "Kvar", "status": "planned", "origin": "trixa2"},
          ]}
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.agent_auth as aa
    import trixa_api.agent_api as ag

    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    ag.get_postgrest = lambda: fake
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ag.router)
    r = TestClient(app).get("/agent/week?monday=2026-09-07", headers=_with_token(st, aa))
    assert [s["title"] for s in r.json()["sessions"]] == ["Kvar"]


# ---------- F5 ----------


def test_eget_pass_pa_upptagen_dag_ger_ingen_500_och_loggar_anda():
    import coach.trixa.db as db
    import trixa_api.ui as ui

    st = {"planned_sessions": [{"id": "nils-1", "user_id": UID, "date": "2026-09-05",
                                "sport": "Löpning", "origin": "nils", "status": "planned"}],
          "training_log": []}
    fake = _C(st)
    db.get_postgrest = lambda: fake
    ui.get_postgrest = lambda: fake
    ui._current_user_id = lambda request: UID
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ui.router)
    c = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    r = c.post("/ui/workouts/custom", data={
        "date": "2026-09-05", "sport": "run", "duration_minutes": "30",
        "description": "Löpning", "already_done": "1"})
    assert r.status_code == 303
    assert "notice=finns" in r.headers["location"]
    assert len(st["planned_sessions"]) == 1          # Nils rad orörd, ingen dubblett
    assert st["training_log"][0]["planned_session_id"] == "nils-1"
