"""Regressionstester för TX-1 och TX-11 ur rapporten 2026-09-01.

TX-1: ``get_athlete`` returnerade en delmängd av profilen. Aktiva discipliner,
pool-tillgång, utrustning, vilodagar och långpassdagar saknades helt — precis de
fält som avgör vad som går att lägga. Utfallet i skarp drift blev en 35-veckors
triathlonplan åt en adept vars sim och cykel var avstängda, med sex pass på
låsta vilodagar. Nitton pass fick raderas.

TX-11: ``get_recovery`` svarade tomt utan att säga att tomt är normalläget för
en adept utan klocka, och serverinstruktionen sa inget om vad agenten gör då.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.tests.test_agent_api import UID, _C, _with_token  # noqa: E402

# En adept som bara springer och lyfter: sim och cykel avstängda, ingen pool,
# tisdag och torsdag låsta som vila, och ett knäbesvär som stänger löpning helt.
FULL_ATHLETE = {
    "id": "81b667bc",
    "user_id": UID,
    "coach_name": "Nils",
    "goal": "first_race",
    "experience_level": "beginner",
    "weekly_hours": 5,
    "weekly_days": 3,
    "sports": ["run", "strength"],
    "preferred_rest_days": ["tuesday", "thursday"],
    "long_run_day": "sunday",
    "long_bike_day": None,
    "equipment": {"pool_type": "none", "has_treadmill": True},
    "preferred_settings": {"run": "outdoor"},
    "active_concerns": [
        {"name": "Knä", "impact_per_discipline": {"run": "partial", "strength": "none"}},
        {"name": "Hälsena", "impact_per_discipline": {"run": "full"}},
    ],
    "garmin_athlete_id": None,
}


def _client_and_store(athlete: dict | None = None):
    st = {
        "api_tokens": [],
        "profiles": [{"id": UID, "name": "Adept"}],
        "athlete_profiles": [dict(athlete or FULL_ATHLETE)],
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


# ---------- TX-1: hela profilen över MCP-ytan ----------


def test_get_athlete_exposes_the_fields_that_gate_planning():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    body = c.get("/agent/athlete", headers=H).json()
    # Utan de här fälten planerar en agent blint — det var precis vad som hände.
    for field in ("sports", "preferred_rest_days", "long_run_day", "long_bike_day",
                  "equipment", "preferred_settings", "active_concerns", "coach_name"):
        assert field in body, f"{field} saknas i get_athlete"
    assert body["sports"] == ["run", "strength"]
    assert body["equipment"]["pool_type"] == "none"


def test_constraints_blocks_inactive_and_injured_disciplines():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    body = c.get("/agent/constraints", headers=H).json()

    assert body["sports"] == ["run", "strength"]
    assert set(body["inactive_sports"]) == {"swim", "bike"}
    # Hälsenan stänger löpning helt; knäet väger lättare och ska inte vinna.
    assert body["discipline_impact"]["run"] == "full"
    assert body["blocked_sports"] == ["run"]
    assert body["plannable_sports"] == ["strength"]
    assert body["rest_days"] == ["tuesday", "thursday"]
    assert body["pool_access"] == "none"
    assert body["long_session_days"] == {"bike": None, "run": "sunday"}
    assert any("vilodagar" in r.lower() for r in body["reasons"])


def test_constraints_blocks_swim_without_pool_access():
    athlete = dict(FULL_ATHLETE, sports=["swim", "run"], active_concerns=[])
    c, st, aa = _client_and_store(athlete)
    H = _with_token(st, aa)
    body = c.get("/agent/constraints", headers=H).json()
    # Simning är påslagen men det finns ingen pool och inget öppet vatten.
    assert "swim" in body["blocked_sports"]
    assert body["plannable_sports"] == ["run"]


def test_constraints_partial_impact_limits_without_blocking():
    athlete = dict(
        FULL_ATHLETE,
        active_concerns=[{"name": "Knä", "impact_per_discipline": {"run": "partial"}}],
    )
    c, st, aa = _client_and_store(athlete)
    H = _with_token(st, aa)
    body = c.get("/agent/constraints", headers=H).json()
    assert body["limited_sports"] == ["run"]
    assert body["blocked_sports"] == []
    assert "run" in body["plannable_sports"]


def test_constraints_defaults_to_all_sports_when_profile_is_empty():
    athlete = {"id": "81b667bc", "user_id": UID, "sports": None,
               "preferred_rest_days": None, "equipment": None,
               "active_concerns": None, "preferred_settings": None}
    c, st, aa = _client_and_store(athlete)
    H = _with_token(st, aa)
    body = c.get("/agent/constraints", headers=H).json()
    assert body["sports"] == ["swim", "bike", "run", "strength"]
    assert body["blocked_sports"] == []
    assert body["rest_days"] == []


# ---------- TX-11: tomt återhämtningssvar ska säga att tomt är normalt ----------


def test_recovery_without_watch_says_so_explicitly():
    c, st, aa = _client_and_store()
    H = _with_token(st, aa)
    body = c.get("/agent/recovery", headers=H).json()
    assert body["metrics"] == []
    assert body["has_data"] is False
    assert "kopplad klocka" in body["note"]


def test_recovery_with_watch_but_no_rows_is_not_read_as_good_values():
    athlete = dict(FULL_ATHLETE, garmin_athlete_id="g1")
    c, st, aa = _client_and_store(athlete)
    H = _with_token(st, aa)
    body = c.get("/agent/recovery", headers=H).json()
    assert body["has_data"] is False
    assert body["note"]


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
