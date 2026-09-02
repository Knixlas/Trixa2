"""Effektivitetsfynden ur kodöversynen 2026-09-02 (docs/12, avsnitt H).

H1  Passbanken parsades om från disk vid varje anrop (~250 ms × 2–4/klick).
H2  Varje /ui-anrop gjorde ett HTTP-anrop till Supabase Auth.
H3  Datalistan hämtades med .limit(500) utan sortering.
H5  Jinja-cachen var avstängd.
H6  last_used_at skrevs per verktygsanrop; strength-historik hämtades
    även för veckor utan styrkepass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

from coach.engine import loader  # noqa: E402


# ---------- H1 ----------


def test_passbanken_parsas_en_gang_och_kopieras_ut():
    loader.clear_workout_cache()
    a = loader.load_workouts()
    b = loader.load_workouts()
    assert a and a == b
    assert a is not b and a[0] is not b[0]        # djup kopia: mutation läcker inte
    a[0]["code"] = "MUTERAD"
    assert loader.load_workouts()[0]["code"] != "MUTERAD"
    assert loader._load_workouts_cached.cache_info().hits >= 2


def test_katalogerna_cachas_ocksa():
    loader.clear_workout_cache()
    loader.load_strength_exercises(); loader.load_strength_exercises()
    loader.load_drills(); loader.load_drills()
    assert loader._load_catalogue_cached.cache_info().hits >= 2


# ---------- H2 ----------


def test_verifierad_session_slipper_nytt_http_anrop(monkeypatch):
    import trixa_api.supabase_auth as sa

    calls = {"n": 0}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "user-1"}

    def fake_get(*_a, **_k):
        calls["n"] += 1
        return _R()

    monkeypatch.setattr(sa.requests, "get", fake_get)
    sa._VERIFIED.clear()
    assert sa.get_user_id("tok-abc") == "user-1"
    assert sa.get_user_id("tok-abc") == "user-1"
    assert calls["n"] == 1                          # andra anropet ur cachen


def test_ogiltig_token_cachas_inte(monkeypatch):
    import trixa_api.supabase_auth as sa

    class _R:
        status_code = 401

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(sa.requests, "get", lambda *a, **k: _R())
    sa._VERIFIED.clear()
    assert sa.get_user_id("tok-bad") is None
    assert "tok-bad" not in {k for k in sa._VERIFIED}


# ---------- H5 ----------


def test_jinja_cachen_ar_pa():
    import trixa_api.ui as ui

    assert ui._jinja_env.cache is not None


# ---------- H6 ----------


def test_last_used_skrivs_bara_nar_det_ar_gammalt():
    from datetime import datetime, timedelta, timezone

    from coach.tests.test_agent_api import UID, _C, _with_token
    import coach.trixa.db as db
    import trixa_api.agent_auth as aa

    st = {"api_tokens": [], "profiles": [{"id": UID}]}
    fake = _C(st)
    db.get_postgrest = lambda: fake
    aa.get_postgrest = lambda: fake
    headers = _with_token(st, aa)
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    st["api_tokens"][0]["last_used_at"] = fresh

    import trixa_api.agent_api as ag
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    ag.get_postgrest = lambda: fake
    app = FastAPI()
    app.include_router(ag.router)
    c = TestClient(app)
    assert c.get("/agent/whoami", headers=headers).status_code == 200
    assert st["api_tokens"][0]["last_used_at"] == fresh          # inte omskriven

    st["api_tokens"][0]["last_used_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    c.get("/agent/whoami", headers=headers)
    assert st["api_tokens"][0]["last_used_at"] != fresh          # nu uppdaterad
