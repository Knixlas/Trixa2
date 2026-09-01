"""Tester för TX-4 ur rapporten 2026-09-01.

Loggformuläret (``exercise_logs``) tar emot namn, set, reps, vikt och
ansträngning, men det planerade passet bar bara fritext. Det fanns ingen
koppling mellan plan och logg — adepten skrev in varje övningsnamn för hand
trots att Trixa redan visste exakt vad passet innehöll. Ett benpass med tolv
övningar betydde tolv manuella inmatningar av data appen själv genererat.

Gränsen som inte får flyttas: förifyll FORMULÄRET, skriv aldrig loggraden.
Att registrera pass som inte utförts förstör datakvaliteten motorn vilar på.
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
    exercises_from_steps,
    normalize_exercises,
)

STEPS = [
    {"segment": "warmup", "type": "general", "duration_min": 8,
     "description": "Lätt cykel"},
    {"segment": "strength_block", "order": 1, "exercise": "back_squat",
     "prescription": {"sets": 3, "reps": 5, "rir": 2, "rest_sec": 180},
     "load_pct": "80% 1RM", "alt": "goblet_squat", "note": "Fullt djup."},
    {"segment": "strength_block", "order": 2, "exercise": "romanian_deadlift",
     "prescription": {"sets": {"range": [2, 4], "default": 3}, "reps": 8},
     "load_pct": "kroppsvikt"},
    {"segment": "cooldown", "type": "mobility", "duration_min": 5},
]


# ---------- härledning ur passbankens main_set ----------


def test_only_strength_blocks_become_loggable_exercises():
    out = exercises_from_steps(STEPS)
    # Uppvärmning och nedvarvning har inga set och reps att bekräfta.
    assert [e["code"] for e in out] == ["back_squat", "romanian_deadlift"]


def test_prescription_is_carried_into_the_form_fields():
    squat = exercises_from_steps(STEPS)[0]
    assert (squat["sets"], squat["reps"], squat["rir"]) == (3, 5, 2)
    assert squat["rest_sec"] == 180
    assert squat["load"] == "80% 1RM"
    assert squat["note"] == "Fullt djup."


def test_template_ranges_collapse_to_their_default():
    # Passbanken skriver {"range": [2, 4], "default": 3} — formuläret vill ha 3.
    rdl = exercises_from_steps(STEPS)[1]
    assert rdl["sets"] == 3


def test_catalogue_names_win_over_the_raw_code():
    out = exercises_from_steps(STEPS, {"back_squat": {"name": "Knäböj bakom nacke"}})
    assert out[0]["name"] == "Knäböj bakom nacke"
    # Utan katalogpost blir koden läsbar i stället för att läcka snake_case.
    assert out[1]["name"] == "Romanian deadlift"


def test_missing_or_broken_steps_give_an_empty_list():
    assert exercises_from_steps(None) == []
    assert exercises_from_steps([{"segment": "warmup"}, "skräp"]) == []


# ---------- övningar från en extern skrivare (AI-coach) ----------


def test_normalize_keeps_known_fields_and_drops_nameless_rows():
    out = normalize_exercises([
        {"name": "Knäböj", "sets": "3", "reps": 8, "weight_from": "60.5"},
        {"sets": 3},                       # utan namn finns inget att bocka av
        "skräp",
    ])
    assert len(out) == 1
    assert out[0]["sets"] == 3 and out[0]["weight_from"] == 60.5


def test_normalize_survives_junk_values():
    out = normalize_exercises([{"name": "Utfall", "sets": "många", "weight_from": "tungt"}])
    assert out[0]["sets"] is None and out[0]["weight_from"] is None


# ---------- skriv- och läsvägen över agent-API:t ----------


def _client_and_store():
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Adept"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID}],
        "planned_sessions": [],
        "training_log": [],
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
    return TestClient(app, raise_server_exceptions=False), st, aa


def test_plan_session_stores_and_returns_exercises():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-09-07", "sport": "strength", "title": "Ben",
        "exercises": [{"name": "Knäböj", "sets": 3, "reps": 5, "weight_from": 80}],
    })
    assert r.status_code == 200, r.text
    assert st["planned_sessions"][0]["exercises"][0]["name"] == "Knäböj"

    week = c.get("/agent/week", headers=H, params={"monday": "2026-09-07"}).json()
    assert week["sessions"][0]["exercises"][0]["sets"] == 3


def test_session_without_exercises_stores_null_not_an_empty_list():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    c.post("/agent/plan/session", headers=H,
           json={"date": "2026-09-08", "sport": "run", "title": "Distans"})
    assert st["planned_sessions"][0]["exercises"] is None


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
