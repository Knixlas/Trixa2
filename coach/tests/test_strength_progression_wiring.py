"""Progressionen hela vägen: logg i databasen → förslag i formulär och API.

Räknemodellen testas i ``test_strength_progression``. Här testas kopplingen —
att loggen faktiskt läses tillbaka, att koden följer med in i databasen, och
att adeptens app och coachen ser samma tal. En perfekt progressionsmotor som
ingen matar med historik är fortfarande en tom ruta i gymmet.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TRIXA_ALLOW_NO_AUTH", "1")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402

SQUAT = {
    "code": "back_squat", "name": "Knäböj", "sets": 3, "reps": 5,
    "reps_min": 3, "reps_max": 6, "rir": 2, "load": "80% 1RM",
}


def _log(date, weight=60.0, reps=6, effort=1):
    return {
        "id": f"log-{date}", "user_id": UID, "session_date": date,
        "exercise_name": "Knäböj", "exercise_code": "back_squat",
        "sets": 3, "reps": reps, "weight_from": weight, "effort": effort,
    }


def _week(date="2026-09-09"):
    return {
        "week_start": "2026-09-07", "week_end": "2026-09-13",
        "workouts": [{
            "date": date, "sport": "strength", "title": "Ben",
            "planned_exercises": [dict(SQUAT)],
        }],
    }


def _ui_with(logs):
    import coach.trixa.db as db
    import trixa_api.ui as ui

    fake = _C({"exercise_logs": [dict(lg) for lg in logs]})
    db.get_postgrest = lambda: fake
    ui.get_postgrest = lambda: fake
    ui._current_user_id = lambda request: UID
    return ui, fake


# ---------- adeptens formulär ----------


def test_formularet_forifylls_med_nasta_vikt_ur_loggen():
    ui, fake = _ui_with([_log("2026-09-02")])
    week = _week()
    ui._attach_strength_logs(fake, week, UID)

    ex = week["workouts"][0]["exercises_to_log"][0]
    assert ex["weight_from"] == 62.5      # 60 kg × 6 reps kändes lätt → upp
    assert ex["reps"] == 3                # tillbaka till spannets golv
    assert "Förra gången" in ex["suggestion"]["reason"]


def test_redan_bockad_ovning_forsvinner_ur_listan():
    """Loggen är kvittot. Det som är avbockat ska inte be om avbockning igen."""
    ui, fake = _ui_with([_log("2026-09-02"), _log("2026-09-09", effort=2)])
    week = _week()
    ui._attach_strength_logs(fake, week, UID)

    assert week["workouts"][0]["exercises_to_log"] == []
    assert len(week["workouts"][0]["logged_exercises"]) == 1


def test_senare_pass_i_veckan_styr_inte_ett_tidigare_bakat():
    """Onsdagens logg får inte räknas som underlag för måndagens pass — den
    fanns inte när måndagen kördes."""
    ui, fake = _ui_with([_log("2026-09-11", weight=80.0)])
    week = _week(date="2026-09-09")
    ui._attach_strength_logs(fake, week, UID)

    ex = week["workouts"][0]["exercises_to_log"][0]
    assert ex["suggestion"]["trend"] == "new"
    assert ex.get("weight_from") is None


def test_utan_historik_star_vikten_tom_i_stallet_for_gissad():
    ui, fake = _ui_with([])
    week = _week()
    ui._attach_strength_logs(fake, week, UID)

    ex = week["workouts"][0]["exercises_to_log"][0]
    assert ex.get("weight_from") is None
    assert "reps i tanken" in ex["suggestion"]["reason"]


# ---------- avbockningen skriver det progressionen behöver ----------


def test_avbockning_skriver_ovningskoden():
    """Utan koden tappar historiken övningen så fort katalogens namn ändras.

    _check_columns i den falska klienten speglar de riktiga kolumnerna, så
    testet fallerar också om migration 013 inte finns i databasen.
    """
    ui, fake = _ui_with([])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ui.router)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r = client.post("/ui/strength/log", data={
        "session_date": "2026-09-09", "exercise_name": "Knäböj",
        "exercise_code": "back_squat", "sets": 3, "reps": 5,
        "weight_from": 62.5, "effort": 2,
    })
    assert r.status_code == 303, r.text
    row = fake.st["exercise_logs"][0]
    assert row["exercise_code"] == "back_squat"
    assert row["weight_from"] == 62.5


def test_extra_ovning_utan_kod_gar_fortfarande_att_logga():
    ui, fake = _ui_with([])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ui.router)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    r = client.post("/ui/strength/log", data={
        "session_date": "2026-09-09", "exercise_name": "Chins", "effort": 3,
    })
    assert r.status_code == 303, r.text
    assert fake.st["exercise_logs"][0]["exercise_code"] is None


# ---------- coachen ser samma tal ----------


def test_agent_week_bar_samma_forslag_som_appen():
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Adept"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
        "planned_sessions": [{
            "id": "ps-1", "user_id": UID, "date": "2026-09-09", "sport": "Styrka",
            "title": "Ben", "exercises": [dict(SQUAT)], "origin": "trixa2",
            "status": "planned",
        }],
        "exercise_logs": [_log("2026-09-02")],
    }
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
    client = TestClient(app, raise_server_exceptions=False)

    week = client.get(
        "/agent/week", headers=_with_token(st, aa), params={"monday": "2026-09-07"}
    ).json()
    ex = week["sessions"][0]["exercises"][0]
    assert ex["weight_from"] == 62.5
    assert ex["suggestion"]["trend"] == "up"


def _run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"✗ {name}: {e}")
        return False
    print(f"✓ {name}")
    return True


if __name__ == "__main__":
    ok = True
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            ok &= _run(name, fn)
    print("\n✓ ALLT GRÖNT" if ok else "\n✗ NÅGOT FALLERADE")
    raise SystemExit(0 if ok else 1)
