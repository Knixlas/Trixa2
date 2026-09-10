"""Styrkepass skrivna som prosa ska ändå gå att bocka av (2026-09-10).

Nils skrev torsdagens pass som "Samma vikter som 3/9, alla 2x10: benpress
83, höftabduktion 40, …" och lördagens som "Samma vikter som torsdag" —
trots varningen i plan_session. Adepten stod utan avbockning igen, med
en komplett logg från 3/9 som progressionen inte kunde använda.

Tre reserver, i ordning: tolka prosan (bara igenkända mönster), ärv förra
styrkepassets lista, och matcha coachens kortnamn mot loggens fulla.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402
from coach.trixa.exercise_plan import (  # noqa: E402
    exercises_from_prose,
    planned_exercises,
    previous_strength_session,
)
from coach.trixa.strength_progression import apply_suggestions  # noqa: E402

NILS_THU = ("Samma vikter som 3/9, alla 2x10: benpress 83, höftabduktion 40, bencurl 15, "
            "rodd 20, vadpress 25, Pallof 9, hantellyft 5, hantelpress 5.\n\n"
            "Stanna 3-4 reps från utmattning. Logga i exercise_logs.")
NILS_SAT = ("Andra styrkepasset i veckan. Samma vikter som torsdag. Logga i exercise_logs.\n\n"
            "Dagen före söndagens löpning — inget som lämnar benen tunga.")
SARAH = ('ÖVNINGAR — kopiera namn, set och reps rakt in i "Logga styrka". '
         "1) Dödhäng · 4 set × till nära utmattning · kroppsvikt — 60 sek vila. "
         "2) Enarmsrodd med hantel · 3 × 10 per arm · hantel — RIR 4, 60 sek vila.")
BULLETS = "- **Knäböj med kroppsvikt:** 2×15, RIR 4, 60s vila\n   - _Teknikfokus._\n- **Planka:** 2×30–45s, 45s vila"

LOG_0903 = [
    {"session_date": "2026-09-03", "exercise_name": "Benpress (maskin)",
     "exercise_code": "leg_press_machine", "sets": 2, "reps": 10, "weight_from": 83.0, "effort": 1},
    {"session_date": "2026-09-03", "exercise_name": "Hantellyft till axelhöjd",
     "exercise_code": None, "sets": 2, "reps": 10, "weight_from": 5.0, "effort": 2},
    {"session_date": "2026-09-03", "exercise_name": "Vadpress",
     "exercise_code": None, "sets": 2, "reps": 10, "weight_from": 25.0, "effort": 2},
]


# ---------- prosa → lista ----------


def test_nils_kompakta_form_tolkas():
    out = exercises_from_prose(NILS_THU)
    names = [e["name"] for e in out]
    assert names == ["Benpress", "Höftabduktion", "Bencurl", "Rodd", "Vadpress",
                     "Pallof", "Hantellyft", "Hantelpress"]
    assert all(e["sets"] == 2 and e["reps"] == 10 for e in out)
    assert out[0]["weight_from"] == 83.0 and out[0]["code"] == "leg_press_machine"
    assert out[5]["code"] == "pallof_press"
    assert out[1]["code"] is None                       # höftabduktion finns inte i banken


def test_bara_samma_som_torsdag_ger_tom_lista():
    assert exercises_from_prose(NILS_SAT) == []


def test_numrerad_och_bullets_tolkas_som_backfillen():
    dead_hang, row = exercises_from_prose(SARAH)
    assert dead_hang["reps"] is None and dead_hang["sets"] == 4     # tid, inte reps
    assert row["reps"] == 10 and row["rir"] == 4
    squat, plank = exercises_from_prose(BULLETS)
    assert (squat["sets"], squat["reps"], squat["code"]) == (2, 15, "bodyweight_squat")
    assert plank["reps"] is None and plank["code"] == "plank"


def test_okand_text_ger_tom_lista_inte_gissning():
    assert exercises_from_prose("Kör gymmet som vanligt, känn efter.") == []
    assert exercises_from_prose(None) == []


# ---------- kedjan ----------


def _row(date, details, **kw):
    return {"date": date, "sport": "Styrka", "details": details, "exercises": None,
            "steps": None, "status": "planned", **kw}


def test_planned_exercises_tolkar_prosa_och_marker_det():
    out = planned_exercises(_row("2026-09-10", NILS_THU))
    assert len(out) == 8 and all(e["derived"] == "details" for e in out)


def test_planned_exercises_arver_forra_passet():
    rows = [_row("2026-09-10", NILS_THU), _row("2026-09-12", NILS_SAT)]
    prev = previous_strength_session(rows, "2026-09-12")
    assert prev and prev["date"] == "2026-09-10"
    out = planned_exercises(rows[1], previous=prev)
    assert [e["name"] for e in out][:2] == ["Benpress", "Höftabduktion"]
    assert all(e["derived"] == "previous:2026-09-10" for e in out)


def test_arv_gar_inte_langre_an_tre_veckor():
    rows = [_row("2026-08-01", NILS_THU), _row("2026-09-12", NILS_SAT)]
    assert previous_strength_session(rows, "2026-09-12") is None


def test_riktig_lista_vinner_over_prosa():
    row = _row("2026-09-10", NILS_THU, exercises=[{"name": "Coachens egen"}])
    assert planned_exercises(row) == [{"name": "Coachens egen"}]


# ---------- kortnamn → historik ----------


def test_kortnamn_hittar_loggens_fulla_namn_och_progressionen_raknar():
    planned = planned_exercises(_row("2026-09-10", NILS_THU))
    out = apply_suggestions(planned, LOG_0903, coach_prescribed=True)
    by = {e["name"]: e for e in out}
    assert by["Benpress"]["suggestion"]["trend"] == "up"        # kod-matchning, kändes lätt
    assert by["Hantellyft"]["suggestion"]["previous"]["weight_from"] == 5.0   # delsträng
    assert by["Vadpress"]["suggestion"]["previous"]["weight_from"] == 25.0    # exakt
    assert by["Höftabduktion"]["suggestion"]["trend"] == "new"              # ingen logg


# ---------- plan_session ----------


def test_plan_session_tolkar_prosa_och_varnar():
    st = {"api_tokens": [], "profiles": [{"id": UID}],
          "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
          "planned_sessions": [], "exercise_logs": []}
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
    c = TestClient(app)
    r = c.post("/agent/plan/session", headers=_with_token(st, aa), json={
        "date": "2026-09-10", "sport": "strength", "title": "Styrka 25 min — maskiner",
        "details": NILS_THU})
    assert r.status_code == 200, r.text
    assert len(st["planned_sessions"][0]["exercises"]) == 8
    assert "TOLKAD" in r.json()["warnings"][0]
