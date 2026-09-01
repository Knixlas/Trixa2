"""Tester för TX-5 ur rapporten 2026-09-01.

/ui/health hade exakt två formulär: add och remove. Ett befintligt besvär gick
inte att ändra. Att justera impact_run från none till partial — en ren
schemaläggningsinställning — krävde att den medicinska posten raderades och
skapades om, varpå since_date och historik gick förlorade. Adepten bad
uttryckligen om att medicinska poster inte fick ändras, så justeringen kunde
inte göras alls trots att hon ville ha den.
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

CONCERN = {
    "id": "c0ffee112233",
    "name": "Hälsena höger",
    "location": "ankle_right",
    "locations": ["ankle_right"],
    "severity": 3,
    "since_date": "2026-07-14",
    "needs_followup": True,
    "follow_up_by": "fysio",
    "notes": "Värst på morgonen.",
    "impact_per_discipline": {"swim": "none", "bike": "none", "run": "none",
                              "strength": "none"},
    # Ett fält formuläret inte känner till — får inte försvinna vid redigering.
    "reported_via": "chat",
}


def _client_and_store(concerns=None):
    st = {
        "athlete_profiles": [{
            "id": "81b667bc", "user_id": UID,
            "active_concerns": [dict(c) for c in (concerns or [CONCERN])],
        }],
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
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False), st


def _concerns(st):
    return st["athlete_profiles"][0]["active_concerns"]


def _edit_form(**overrides):
    form = {
        "concern_id": CONCERN["id"],
        "index": "0",
        "name": CONCERN["name"],
        "locations": ["ankle_right"],
        "severity": "3",
        "since_date": CONCERN["since_date"],
        "impact_swim": "none",
        "impact_bike": "none",
        "impact_run": "none",
        "impact_strength": "none",
        "needs_followup": "1",
        "follow_up_by": "fysio",
        "notes": CONCERN["notes"],
    }
    form.update(overrides)
    return form


def test_impact_can_be_changed_without_losing_the_medical_record():
    c, st = _client_and_store()
    r = c.post("/ui/health/update", data=_edit_form(impact_run="partial"))
    assert r.status_code == 303, r.text

    concerns = _concerns(st)
    assert len(concerns) == 1, "posten ska ändras, inte ersättas"
    updated = concerns[0]
    assert updated["impact_per_discipline"]["run"] == "partial"
    # Det som gick förlorat i delete-och-skapa-om-vägen:
    assert updated["since_date"] == "2026-07-14"
    assert updated["id"] == CONCERN["id"]
    assert updated["reported_via"] == "chat"
    assert updated["updated_at"]


def test_edit_page_prefills_the_existing_values():
    c, st = _client_and_store()
    body = c.get("/ui/health").text
    assert "/ui/health/update" in body
    assert 'value="2026-07-14"' in body
    assert "Värst på morgonen." in body


def test_id_addresses_the_row_even_when_the_index_moved():
    other = dict(CONCERN, id="aaaabbbbcccc", name="Ryggskott")
    c, st = _client_and_store([other, CONCERN])
    # index pekar på rad 0 (Ryggskott) men id:t pekar på Hälsenan — id vinner.
    r = c.post("/ui/health/update", data=_edit_form(index="0", impact_run="full"))
    assert r.status_code == 303
    concerns = _concerns(st)
    assert concerns[0]["name"] == "Ryggskott"
    assert concerns[0]["impact_per_discipline"]["run"] == "none"
    assert concerns[1]["impact_per_discipline"]["run"] == "full"


def test_unknown_id_is_refused_instead_of_writing_the_wrong_row():
    c, st = _client_and_store()
    r = c.post("/ui/health/update", data=_edit_form(concern_id="nope"))
    assert r.status_code == 404
    assert _concerns(st)[0]["impact_per_discipline"]["run"] == "none"


def test_legacy_concern_without_id_is_still_editable_by_index():
    legacy = {k: v for k, v in CONCERN.items() if k != "id"}
    c, st = _client_and_store([legacy])
    r = c.post("/ui/health/update", data=_edit_form(concern_id="", index="0",
                                                    severity="5"))
    assert r.status_code == 303
    assert _concerns(st)[0]["severity"] == 5


def test_new_concerns_get_a_stable_id():
    c, st = _client_and_store([])
    r = c.post("/ui/health/add", data={
        "name": "Knä vänster", "locations": ["knee_left"], "severity": "2",
        "impact_swim": "none", "impact_bike": "none", "impact_run": "partial",
        "impact_strength": "none",
    })
    assert r.status_code == 303, r.text
    assert _concerns(st)[0]["id"]


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
