"""Webben på en gammal adress skickas vidare till den nya.

Trixa flyttade från trixa2.up.railway.app till trixa.sviden.se. Den som har
ett bokmärke eller en ikon på hemskärmen ska hamna rätt, men MCP-kopplingar
mot den gamla adressen ska fortsätta svara: nyckeln är bunden till adressen,
så en vidarebefordran där slår bara ut kopplingen.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from trixa_api.main import app  # noqa: E402

GAMMAL = "gammal.test"
NY = "https://ny.test"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TRIXA_REDIRECT_HOSTS", f"{GAMMAL}, annan-gammal.test")
    monkeypatch.setenv("TRIXA_REDIRECT_TARGET", f"{NY}/")
    monkeypatch.delenv("TRIXA_ALLOW_NO_AUTH", raising=False)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


def _get(client, path, host=GAMMAL, **kwargs):
    return client.get(path, headers={"host": host}, **kwargs)


def test_roten_och_webben_skickas_vidare(client):
    r = _get(client, "/")
    assert r.status_code == 307
    assert r.headers["location"] == f"{NY}/"

    r = _get(client, "/ui/")
    assert r.status_code == 307
    assert r.headers["location"] == f"{NY}/ui/"


def test_sokvag_och_fraga_foljer_med(client):
    r = _get(client, "/ui/week/2026-11-16?visa=allt&dag=3")
    assert r.status_code == 307
    assert r.headers["location"] == f"{NY}/ui/week/2026-11-16?visa=allt&dag=3"


def test_varje_gammal_adress_i_listan_galler(client):
    r = _get(client, "/ui/", host="annan-gammal.test")
    assert r.headers["location"] == f"{NY}/ui/"


def test_varden_bakom_proxyn_lases_ur_forwarded_host(client):
    r = client.get(
        "/ui/", headers={"host": "intern:8080", "x-forwarded-host": GAMMAL.upper()}
    )
    assert r.status_code == 307
    assert r.headers["location"] == f"{NY}/ui/"


def test_nya_adressen_skickas_inte_vidare(client):
    """Annars går den nya adressen runt i en ring."""
    r = _get(client, "/ui/", host="ny.test")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/login")


def test_maskinvagarna_svarar_kvar_pa_gamla_adressen(client):
    assert _get(client, "/health").status_code == 200
    assert _get(client, "/.well-known/oauth-protected-resource").status_code == 200

    r = client.post("/mcp", headers={"host": GAMMAL}, json={})
    assert r.status_code == 401
    assert "location" not in r.headers


def test_formular_fran_en_oppen_sida_gar_inte_vidare(client):
    """Det adepten just fyllde i ska inte tappas på vägen till nya adressen."""
    r = client.post("/ui/log", headers={"host": GAMMAL}, data={"x": "1"})
    assert not r.headers.get("location", "").startswith(NY)


def test_inloggning_mitt_i_oauth_stannar_pa_gamla_adressen(client):
    """Klienten väntar på svar från adressen den började på."""
    r = _get(client, "/ui/login?next=/oauth/authorize%3Fclient_id%3Dabc")
    assert r.status_code == 200

    r = _get(client, "/ui/login")
    assert r.status_code == 307
    assert r.headers["location"] == f"{NY}/ui/login"


def test_utan_variabler_hander_ingenting(client, monkeypatch):
    monkeypatch.delenv("TRIXA_REDIRECT_HOSTS")
    r = _get(client, "/ui/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/ui/login")

    monkeypatch.setenv("TRIXA_REDIRECT_HOSTS", GAMMAL)
    monkeypatch.delenv("TRIXA_REDIRECT_TARGET")
    r = _get(client, "/ui/")
    assert r.headers["location"].startswith("/ui/login")
