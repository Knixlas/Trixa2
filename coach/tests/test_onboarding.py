"""Tester för onboardingen — den generella (icke-tri-antagande) versionen.

Feedbacken efter första skarpa nyregistreringen: formuläret antog att varje ny
användare var en erfaren triatlet. Testerna låser fast att det inte gör det
längre — aktiva discipliner styr vad som sparas, distansen får vara enkelgren,
besvär kan sitta på flera ställen, och coachens namn är adeptens val.

Fejkad postgrest från test_agent_api (underscore-namn → ingen dubbelinsamling).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import os  # noqa: E402

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake")
os.environ.setdefault("TRIXA_ALLOW_NO_AUTH", "1")

from coach.tests.test_agent_api import UID, _C  # noqa: E402


def _client_and_store():
    st = {
        "athlete_profiles": [{
            "id": "81b667bc", "user_id": UID, "goal": "ironman",
            "experience_level": "intermediate", "sports": ["swim", "bike", "run"],
            "weekly_hours": 6, "preferred_rest_days": ["monday"],
            "recovery_week_ratio": "3:1", "active_concerns": [], "health_conditions": [],
        }],
        "races": [],
        "profiles": [{"id": UID, "name": "Testadept"}],
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
    # Följ inte redirects: vi vill se att POST:en faktiskt svarar 303 mot
    # kvittenssidan, inte bara att någon sida till slut renderar.
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False), st


def _base_form(**overrides):
    form = {
        "sports": ["run"],
        "experience_level": "beginner",
        "goal": "first_race",
        "coach_name": "Elin",
        "weekly_hours": "5",
        "recovery_week_ratio": "3:1",
        "race_priority": "A",
    }
    form.update(overrides)
    return form


def _profile(st):
    return st["athlete_profiles"][0]


def test_form_renders_for_new_athlete():
    c, st = _client_and_store()
    r = c.get("/ui/onboarding")
    assert r.status_code == 200, r.text
    body = r.text
    # Grenvalet ska komma före tröskelvärdena — det styr resten av formuläret.
    assert body.index("Aktiva discipliner") < body.index("Testvärden"), "grenvalet ligger fel"
    assert "Nils" in body, "coachnamn ska erbjudas som lista"
    assert "markera alla som gäller" in body, "skadeplats ska vara flerval"


def test_single_sport_athlete_gets_no_bike_long_day():
    c, st = _client_and_store()
    r = c.post("/ui/onboarding", data=_base_form(
        sports=["run"], long_bike_day="saturday", long_run_day="sunday"))
    assert r.status_code == 303, r.text
    p = _profile(st)
    assert p["sports"] == ["run"], p["sports"]
    assert p["long_bike_day"] is None, "cykel-långpass för en ren löpare"
    assert p["long_run_day"] == "sunday"


def test_runner_can_save_a_running_distance():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(
        sports=["run"], race_name="Stockholm Marathon",
        race_date="2027-06-05", race_distance="marathon", race_target="3:45:00"))
    assert st["races"], "tävlingen sparades inte"
    race = st["races"][0]
    assert race["distance"] == "marathon", race["distance"]
    assert _profile(st)["race_type"] == "marathon"


def test_unknown_distance_falls_back_to_other_not_ironman():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(
        race_name="Något lopp", race_date="2027-06-05", race_distance="hittepå"))
    assert st["races"][0]["distance"] == "other", st["races"][0]["distance"]


def test_concern_can_sit_in_several_places():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(
        concern_name_1="Löparknä",
        concern_locations_1=["knee_left", "knee_right"],
        concern_severity_1="3"))
    concerns = _profile(st)["active_concerns"]
    assert len(concerns) == 1, concerns
    assert concerns[0]["locations"] == ["knee_left", "knee_right"], concerns[0]
    # location (singular) lever kvar för äldre läsvägar
    assert concerns[0]["location"] == "knee_left"


def test_several_concerns_and_conditions():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(
        concern_name_1="Hälsena", concern_locations_1=["achilles_right"],
        concern_name_2="Korsrygg", concern_locations_2=["lower_back"],
        condition_name_1="Hypotyreos", condition_medication_1="Levaxin",
        condition_name_2="Astma", condition_medication_2="Bricanyl"))
    p = _profile(st)
    assert [c["name"] for c in p["active_concerns"]] == ["Hälsena", "Korsrygg"]
    assert [c["name"] for c in p["health_conditions"]] == ["Hypotyreos", "Astma"]
    assert p["health_conditions"][0]["medication"] == "Levaxin"


def test_coach_name_is_the_athletes_choice():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(coach_name="Maja"))
    assert _profile(st)["coach_name"] == "Maja"

    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(coach_name="Maja", coach_name_custom="Kajsa"))
    assert _profile(st)["coach_name"] == "Kajsa", "eget namn ska vinna över listan"

    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(coach_name="", coach_name_custom=""))
    assert _profile(st)["coach_name"] is None, "tomt val = neutralt 'din coach'"


def test_empty_sport_selection_falls_back_to_all_three():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(sports=[]))
    assert _profile(st)["sports"] == ["swim", "bike", "run"]


def test_onboarding_marks_version_and_completion():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form())
    p = _profile(st)
    assert p["onboarded_at"], "onboarded_at måste sättas, annars loopar dashboarden"
    assert p["onboarding_version"] >= 1


def test_done_page_reflects_the_answers():
    c, st = _client_and_store()
    c.post("/ui/onboarding", data=_base_form(
        sports=["run"], coach_name="Sam",
        race_name="Lidingöloppet", race_date="2027-09-25", race_distance="ultra",
        concern_name_1="Löparknä", concern_locations_1=["knee_left", "knee_right"]))
    r = c.get("/ui/onboarding/klart")
    assert r.status_code == 200, r.text
    body = r.text
    assert "Sam" in body
    assert "Lidingöloppet" in body and "Ultralopp" in body
    assert "Knä vänster, Knä höger" in body, "flera kroppsdelar ska visas"
    # Utan testvärden ska sidan säga att zonerna uppskattas, inte tiga om det.
    assert "uppskattas" in body


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
