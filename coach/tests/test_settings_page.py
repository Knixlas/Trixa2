"""Tester för TX-8 och TX-10 ur rapporten 2026-09-01.

TX-8: hela Strava- och TrainingPeaks-integrationen var byggd, men gömdes bakom
en kryssruta under "Vad du använder" vars text läste som en deklaration —
"Kryssa i det du faktiskt har kopplat". En adept som ville koppla Strava såg
alltså ingen väg dit alls, och måste först kryssa i att hon redan gjort det hon
försökte göra.

TX-10: sidan hade tre spar-knappar (/ui/settings/profile, /ui/settings/
connections, /ui/settings). Ändrade man i två sektioner och tryckte på en av
dem försvann den andra ändringen utan varning.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TRIXA_ALLOW_NO_AUTH", "1")

from coach.tests.test_agent_api import UID, _C  # noqa: E402

PROFILE = {
    "id": "81b667bc", "user_id": UID,
    "goal": "first_race", "experience_level": "beginner",
    "weekly_hours": 5, "weekly_days": 3, "coach_name": "Nils",
    "race_type": "10k", "race_date": None, "time_goal": None,
    "ftp": None, "lthr": None, "swim_css": None, "run_threshold_pace": None,
    "sports": ["run", "strength"], "preferred_rest_days": ["tuesday"],
    "long_bike_day": None, "long_run_day": "sunday",
    "equipment": {"pool_type": "none"}, "preferred_settings": {},
    "conn_ai": False, "conn_tp": False, "conn_strava": False,
    "use_strava": False, "garmin_athlete_id": None,
}


def _client_and_store():
    st = {
        "athlete_profiles": [dict(PROFILE)],
        "profiles": [{"id": UID, "name": "Testadept"}],
        "api_tokens": [],
        "oauth_tokens": [],
        "oauth_clients": [],
        "tp_auth": [],
        "strava_tokens": [],
        "strava_activities": [],
    }
    fake = _C(st)
    import coach.trixa.db as db
    import trixa_api.ui as ui

    db.get_postgrest = lambda: fake
    ui.get_postgrest = lambda: fake
    ui._current_user_id = lambda request: UID

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(ui.router)
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False), st


def _profile(st):
    return st["athlete_profiles"][0]


def _full_form(**overrides):
    """Det inställningssidan faktiskt postar: alla tre sektionerna på en gång."""
    form = {
        "section": ["profile", "connections", "training"],
        # profil
        "coach_name": "Nils", "goal": "first_race", "experience_level": "beginner",
        "weekly_hours": "5", "weekly_days": "3", "race_type": "10k",
        "race_date": "", "time_goal": "", "ftp": "", "lthr": "",
        "swim_css": "", "run_threshold_pace": "",
        # anslutningar
        "conn_ai": "1",
        # träningsupplägg
        "sports": ["run", "strength"], "long_bike_day": "", "long_run_day": "sunday",
        "rest_days": ["tuesday"], "pool_type": "none",
        "setting_swim": "any", "setting_bike": "any", "setting_run": "any",
    }
    form.update(overrides)
    return form


# ---------- TX-10: en spar-knapp ----------


def test_one_save_writes_every_section():
    c, st = _client_and_store()
    r = c.post("/ui/settings", data=_full_form(
        weekly_hours="9", conn_ai="1", sports=["run"]))
    assert r.status_code == 200, r.text
    p = _profile(st)
    assert p["weekly_hours"] == 9        # profilsektionen
    assert p["conn_ai"] is True          # anslutningssektionen
    assert p["sports"] == ["run"]        # träningssektionen


def test_saving_one_section_does_not_wipe_the_others():
    c, st = _client_and_store()
    # En post som bara bär profilsektionen (den bakåtkompatibla vägen) får inte
    # nolla sports till default eller släcka anslutningsflaggorna.
    _profile(st)["conn_ai"] = True
    r = c.post("/ui/settings", data={
        "section": ["profile"], "weekly_hours": "8",
        "goal": "first_race", "experience_level": "beginner",
    })
    assert r.status_code == 200, r.text
    p = _profile(st)
    assert p["weekly_hours"] == 8
    assert p["sports"] == ["run", "strength"], "träningsupplägget rördes"
    assert p["conn_ai"] is True, "anslutningarna nollades"


def test_legacy_post_without_section_still_saves_training_settings():
    c, st = _client_and_store()
    r = c.post("/ui/settings", data={
        "sports": ["swim"], "rest_days": ["monday"], "pool_type": "25m",
    })
    assert r.status_code == 200, r.text
    p = _profile(st)
    assert p["sports"] == ["swim"]
    assert p["weekly_hours"] == 5, "profilen ska inte röras av en gammal post"


def test_page_has_a_single_settings_form():
    c, st = _client_and_store()
    body = c.get("/ui/settings").text
    assert body.count('action="/ui/settings"') == 1
    assert "Spara om mig" not in body and "Spara anslutningar" not in body
    assert "Spara inställningar" in body


# ---------- TX-8: kopplingsvägen syns ----------


@contextmanager
def _strava_configured(configured: bool = True):
    """Styr Strava-nycklarna i miljön i stället för att ärva dem.

    Utan det här blev testet grönt lokalt (nycklar i .env) och rött i CI
    (inga nycklar) — det mätte utvecklarens miljö, inte koden.
    """
    import trixa_api.strava_client as sc

    saved = (sc.creds_configured, sc.authorize_url, sc.sign_state)
    sc.creds_configured = lambda: configured
    sc.authorize_url = lambda uri, state: "https://www.strava.com/oauth/authorize?x=1"
    sc.sign_state = lambda uid: "state"
    try:
        yield sc
    finally:
        sc.creds_configured, sc.authorize_url, sc.sign_state = saved


def test_connect_paths_are_visible_before_anything_is_checked():
    c, st = _client_and_store()
    with _strava_configured():
        body = c.get("/ui/settings").text
    # Inget ikryssat, inget kopplat — och ändå ska vägen dit finnas.
    assert "Anslut Strava" in body
    assert "Logga in på TrainingPeaks" in body
    assert "Inget kopplat än" in body


def test_missing_strava_keys_explain_themselves_instead_of_hiding_the_section():
    c, st = _client_and_store()
    with _strava_configured(False):
        body = c.get("/ui/settings").text
    # Serverns miljö saknar nycklar — säg det, göm inte hela vägen dit.
    assert "Strava-nycklar saknas" in body
    assert "Logga in på TrainingPeaks" in body


def test_connecting_strava_also_turns_the_capability_on():
    c, st = _client_and_store()
    with _strava_configured():
        r = c.get("/ui/strava/connect")
    assert r.status_code == 303
    assert r.headers["location"].startswith("https://www.strava.com/oauth/authorize")
    assert _profile(st)["conn_strava"] is True


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
