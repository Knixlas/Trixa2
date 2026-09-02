"""Yoga som egen gren, och pass som går att markera gjorda.

Skarp användning 2026-09-02: en adept lade in "Yoga" som eget pass. Yoga
gick inte att välja, så hon valde Styrka för att komma vidare. Morgonen
efter stod passet som "Missad" — och det fanns ingen väg att säga emot.
Styrkepass hade bara övning-för-övning-kvittensen, och statusen läste
aldrig den. Yoga mappade dessutom till vila, så ett genomfört yogapass hade
blivit "Tränade på vilodag".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402
from datetime import date  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C  # noqa: E402

TODAY = date(2026, 9, 2)


def _ui_with(store: dict):
    import coach.trixa.db as db
    import trixa_api.ui as ui

    fake = _C(store)
    db.get_postgrest = lambda: fake
    ui.get_postgrest = lambda: fake
    ui._current_user_id = lambda request: UID
    return ui, fake


def _ui_client(store: dict):
    ui, fake = _ui_with(store)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ui.router)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False), store


# ---------- yoga är en gren ----------


def test_yoga_ar_en_egen_gren_inte_vila():
    import trixa_api.ui as ui

    assert ui._PLANNED_SV_SPORT["Yoga"] == "yoga"
    assert ui._PLANNED_SV_SPORT["Vila"] == "rest"


def test_yogapass_utan_logg_ar_missat_inte_vila_hallen():
    import trixa_api.ui as ui

    status = ui._compute_status("2026-09-01", "yoga", "", 30, [], TODAY)
    assert status["key"] == "missed"


def test_loggat_yogapass_ar_genomfort():
    import trixa_api.ui as ui

    done = ui._normalize_training_log_activity({
        "date": "2026-09-01", "sport": "yoga", "title": "Yoga",
        "duration_min": 30, "source": "manual",
    })
    status = ui._compute_status("2026-09-01", "yoga", "", 30, [done], TODAY)
    assert status["key"] == "done"


# ---------- eget pass kan vara redan gjort ----------


def test_eget_yogapass_gar_att_lagga_in():
    c, st = _ui_client({"planned_sessions": [], "training_log": []})
    r = c.post("/ui/workouts/custom", data={
        "date": "2026-09-01", "sport": "yoga", "duration_minutes": "30",
        "description": "Yoga",
    })
    assert r.status_code == 303
    assert st["planned_sessions"][0]["sport"] == "Yoga"
    assert st["training_log"] == []          # inte gjort förrän adepten säger det


def test_redan_gjort_eget_pass_loggas_direkt():
    """Gårdagens yoga inlagd i efterhand är en rapport, inte en plan."""
    c, st = _ui_client({"planned_sessions": [], "training_log": []})
    c.post("/ui/workouts/custom", data={
        "date": "2026-09-01", "sport": "yoga", "duration_minutes": "30",
        "description": "Yoga", "already_done": "1",
    })
    assert len(st["training_log"]) == 1
    log = st["training_log"][0]
    assert log["sport"] == "yoga"
    assert log["source"] == "manual"
    assert log["duration_min"] == 30


# ---------- markera som gjort ----------


def test_styrka_och_yoga_gar_att_logga_som_pass():
    c, st = _ui_client({"training_log": []})
    for sport in ("strength", "yoga"):
        r = c.post("/ui/log/session", data={
            "date": "2026-09-01", "sport": sport, "duration_min": "30",
        })
        assert r.status_code == 303, (sport, r.text)
    assert {row["sport"] for row in st["training_log"]} == {"strength", "yoga"}


def test_okand_gren_avvisas_fortfarande():
    c, _ = _ui_client({"training_log": []})
    assert c.post("/ui/log/session", data={
        "date": "2026-09-01", "sport": "skidor",
    }).status_code == 400


# ---------- avbockade övningar räknas som utfört pass ----------


def _strength_week(status_key: str) -> dict:
    import trixa_api.ui as ui

    return {
        "week_start": "2026-08-31", "week_end": "2026-09-06",
        "workouts": [{
            "id": "s1", "date": "2026-09-01", "sport": "strength",
            "title": "Ben", "duration_minutes": 45, "planned_exercises": [],
            "status": {**ui._STATUS[status_key], "key": status_key, "actual": None},
        }],
    }


def _exlog(effort: int) -> dict:
    return {
        "id": "e1", "user_id": UID, "session_date": "2026-09-01",
        "exercise_name": "Knäböj", "sets": 3, "reps": 5,
        "weight_from": 60.0, "effort": effort,
    }


def test_avbockade_ovningar_gor_missat_pass_genomfort():
    ui, fake = _ui_with({"exercise_logs": [_exlog(2)]})
    week = _strength_week("missed")
    ui._attach_strength_logs(fake, week, UID)

    w = week["workouts"][0]
    assert w["status"]["key"] == "done"
    assert "1 övning loggad" in w["status"]["actual"]["summary"]


def test_bara_overhoppade_ovningar_raddar_inte_passet():
    """"Hoppade över" är ett kvitto på att passet INTE gjordes."""
    ui, fake = _ui_with({"exercise_logs": [_exlog(-1)]})
    week = _strength_week("missed")
    ui._attach_strength_logs(fake, week, UID)
    assert week["workouts"][0]["status"]["key"] == "missed"


def test_avviken_med_avbockade_ovningar_blir_genomford():
    """"Avviken" på en styrkedag betyder att dagen hade någon ANNAN aktivitet
    och ingen styrkerad i loggen. Avbockade övningar är styrkepassets eget
    kvitto — passet är gjort, cykelturen är en bonus (docs/12 D4)."""
    ui, fake = _ui_with({"exercise_logs": [_exlog(2)]})
    week = _strength_week("deviated")
    ui._attach_strength_logs(fake, week, UID)
    assert week["workouts"][0]["status"]["key"] == "done"


def test_genomford_med_riktig_aktivitet_rors_inte():
    """Finns en riktig styrkeaktivitet i training_log står dess bedömning."""
    ui, fake = _ui_with({"exercise_logs": [_exlog(2)]})
    week = _strength_week("done")
    week["workouts"][0]["status"]["actual"] = {"summary": "Genomfört: 50 min"}
    ui._attach_strength_logs(fake, week, UID)
    assert week["workouts"][0]["status"]["actual"]["summary"] == "Genomfört: 50 min"
