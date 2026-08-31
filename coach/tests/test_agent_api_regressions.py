"""Regressionstester för två buggar en adept hittade i skarp drift (2026-08-31).

Båda passerade den gamla testsviten därför att den fejkade postgrest-klienten
var mer tillåtande än databasen: den svalde okända kolumnnamn och kände inte
till unik-index. Testdubbeln känner numera till båda (se ``test_agent_api``),
och de här testerna låser fast beteendet.

1. ``plan_session`` dokumenterades som upsert på (adept, datum, gren) men letade
   bara efter sina egna rader (``origin='nils'``). Låg motorns rad på samma
   dag och gren slog insert:en i UNIQUE (user_id, date, sport) och adepten fick
   ett rått databasfel — i praktiken varje gång coachen justerade en genererad
   vecka.

2. ``log_override`` krävde en mänsklig coach i ``coach_athletes`` och gav annars
   404. Verktyget var alltså exponerat men omöjligt att använda för varje
   självcoachad adept. Dessutom skrev det ``week_id``/``workout_id`` — kolumner
   som aldrig funnits i ``coach_overrides``, så anropet gick inte igenom för
   någon, ens med coach kopplad.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402

COACH_UID = "4e225307-ee66-4bf8-a141-69f52218e2ce"


def _client_and_store(with_coach: bool = True):
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Adept"}],
        "athlete_profiles": [{"id": "81b667bc", "user_id": UID, "goal": "first_race"}],
        "planned_sessions": [],
        "training_log": [],
        "coach_overrides": [],
        "coach_athletes": (
            [{"athlete_id": UID, "coach_id": COACH_UID, "status": "accepted"}]
            if with_coach else []
        ),
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
    return TestClient(app, raise_server_exceptions=False), st, aa, fake


# ---------- bugg 1: plan_session ----------


def test_plan_session_takes_over_engine_row_instead_of_crashing():
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    # Motorn har redan lagt ett pass den dagen och grenen.
    st["planned_sessions"].append({
        "id": "gen-1", "user_id": UID, "date": "2026-09-02", "sport": "Cykel",
        "title": "Genererat Z2", "origin": "trixa2", "status": "planned",
    })

    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-09-02", "sport": "bike", "title": "Tröskel 3x10",
        "duration_min": 75})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert body["replaced_origin"] == "trixa2", body

    # Samma rad, övertagen — inte en dubblett och inte ett fel.
    assert len(st["planned_sessions"]) == 1
    row = st["planned_sessions"][0]
    assert row["id"] == "gen-1", "raden ska behålla sitt id (training_log länkar dit)"
    assert row["title"] == "Tröskel 3x10"
    assert row["origin"] == "nils", "övertagen rad skyddas från nästa regenerering"


def test_plan_session_takes_over_athletes_own_row():
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    st["planned_sessions"].append({
        "id": "man-1", "user_id": UID, "date": "2026-09-03", "sport": "Löpning",
        "title": "Eget pass", "origin": "manual", "status": "planned",
    })
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-09-03", "sport": "run", "title": "Långpass"})
    assert r.json()["replaced_origin"] == "manual", r.json()
    assert len(st["planned_sessions"]) == 1


def test_plan_session_survives_a_race_on_the_unique_index():
    """Raden dyker upp mellan uppslaget och insert:en."""
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    st["planned_sessions"].append({
        "id": "gen-2", "user_id": UID, "date": "2026-09-04", "sport": "Sim",
        "title": "Genererat", "origin": "trixa2", "status": "planned",
    })

    # Låt första uppslaget se en tom tabell — då försöker koden insert, slår i
    # unik-indexet och måste läsa om.
    real_table = fake.table
    state = {"first": True}

    def table(name):
        q = real_table(name)
        if name == "planned_sessions" and state["first"]:
            real_execute = q.execute

            def execute():
                if q._ins is None and q._u is None and not q._del:
                    state["first"] = False
                    return type("R", (), {"data": []})()
                return real_execute()

            q.execute = execute
        return q

    fake.table = table
    try:
        r = c.post("/agent/plan/session", headers=H, json={
            "date": "2026-09-04", "sport": "swim", "title": "CSS 5x200"})
    finally:
        fake.table = real_table

    assert r.status_code == 200, r.text
    assert r.json()["id"] == "gen-2", r.json()
    assert len(st["planned_sessions"]) == 1, "ingen dubblett trots kapplöpningen"


def test_plan_session_still_creates_when_the_day_is_free():
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/plan/session", headers=H, json={
        "date": "2026-09-05", "sport": "run", "title": "Distans"})
    assert r.json()["replaced_origin"] is None, r.json()
    assert st["planned_sessions"][0]["origin"] == "nils"


# ---------- bugg 2: log_override ----------


def test_override_works_without_a_human_coach():
    c, st, aa, fake = _client_and_store(with_coach=False)
    H = _with_token(st, aa)
    r = c.post("/agent/override", headers=H, json={
        "scope": "volume", "engine_recommendation": {"h": 10},
        "override_decision": {"h": 7}, "motivation": "adepten är sjuk i influensa"})
    assert r.status_code == 200, r.text
    assert r.json()["self_coached"] is True, r.json()
    row = st["coach_overrides"][0]
    assert row["coach_user_id"] == UID, "självcoachad: adepten står som beslutsfattare"
    assert row["athlete_id"] == "81b667bc", "athlete_profiles.id, inte user_id"


def test_override_uses_the_human_coach_when_there_is_one():
    c, st, aa, fake = _client_and_store(with_coach=True)
    H = _with_token(st, aa)
    r = c.post("/agent/override", headers=H, json={
        "scope": "phase", "engine_recommendation": {"phase": "build"},
        "override_decision": {"phase": "base_3"}, "motivation": "grunden är för tunn än"})
    assert r.json()["self_coached"] is False, r.json()
    assert st["coach_overrides"][0]["coach_user_id"] == COACH_UID


def test_override_writes_columns_that_actually_exist():
    """Den gamla koden skrev week_id/workout_id — kolumner som inte finns."""
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/override", headers=H, json={
        "scope": "week", "week_start": "2026-09-07",
        "engine_recommendation": {"hours": 12}, "override_decision": {"hours": 9},
        "motivation": "resvecka, planera nedskalat"})
    assert r.status_code == 200, r.text
    row = st["coach_overrides"][0]
    assert row["week_start"] == "2026-09-07", row
    assert "week_id" not in row and "workout_id" not in row


def test_override_workout_scope_uses_planned_session_id():
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    r = c.post("/agent/override", headers=H, json={
        "scope": "workout", "planned_session_id": "sess-1",
        "engine_recommendation": {"code": "AC1"}, "override_decision": {"code": "AE2"},
        "motivation": "axeln tål inte intensiteten i dag"})
    assert r.status_code == 200, r.text
    assert st["coach_overrides"][0]["planned_session_id"] == "sess-1"


def test_override_scope_requires_its_target():
    """DB:s scope_matches_target ska fångas som 400, inte som ett CHECK-fel."""
    c, st, aa, fake = _client_and_store()
    H = _with_token(st, aa)
    base = {"engine_recommendation": {}, "override_decision": {},
            "motivation": "tillräckligt lång motivering"}
    assert c.post("/agent/override", headers=H,
                  json={**base, "scope": "week"}).status_code == 400
    assert c.post("/agent/override", headers=H,
                  json={**base, "scope": "workout"}).status_code == 400
    assert st["coach_overrides"] == []


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
