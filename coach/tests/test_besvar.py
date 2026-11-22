"""Besvär påverkar planen även när påverkan inte valts uttryckligen (docs/13).

Onboardingen skrev impact_per_discipline = {} och hälsosidan förvalde "none",
så ett ont knä med grad 2 ändrade ingenting — en 62-åring med knäbesvär fick
5,7 h löpning vecka 1.
"""

from __future__ import annotations

from datetime import timedelta

from coach.trixa import injuries
from coach.tests.test_levels import MONDAY, TODAY, _plan, _store, _training


def _knee(sev, impact=None):
    return {"name": "Ont i knät", "locations": ["knee_left"], "severity": sev,
            "impact_per_discipline": impact if impact is not None else {}}


def test_knat_grad_2_ger_lugna_lopppass_men_cykel_som_vanligt():
    out = injuries.effective_impacts([_knee(2)])
    assert out["run"] == "partial"
    assert out["bike"] == "none"


def test_knat_grad_4_vilar_lopningen_och_begransar_cykeln():
    out = injuries.effective_impacts([_knee(4)])
    assert out["run"] == "full"
    assert out["bike"] == "partial"


def test_axeln_paverkar_simning_och_styrka_inte_lopning():
    out = injuries.effective_impacts([{"name": "Axel", "location": "shoulder_right", "severity": 3}])
    assert out["swim"] == "partial" and out["strength"] == "partial" and out["run"] == "none"


def test_uttryckligt_val_galler_och_auto_bedoms():
    assert injuries.effective_impacts([_knee(4, {"run": "none"})])["run"] == "none"
    assert injuries.effective_impacts([_knee(4, {"run": "auto"})])["run"] == "full"
    info = injuries.concern_impacts(_knee(2, {"swim": "partial"}))
    assert info["swim"] == {"impact": "partial", "source": "explicit"}
    assert info["run"] == {"impact": "partial", "source": "auto"}


def test_grad_1_paverkar_inget():
    assert set(injuries.effective_impacts([_knee(1)]).values()) == {"none"}


# ---------- planeraren ----------


def _with_concern(concern, **kw):
    st = _store(**kw)
    st["athlete_profiles"][0]["active_concerns"] = [concern]
    return st


def test_planen_sager_vad_besvaret_gor(monkeypatch):
    plan = _plan(monkeypatch, _with_concern(_knee(2)))
    assert any("ont i knät (grad 2)" in w and "bara lugna pass i löpning" in w for w in plan.warnings)
    assert all(w.category == "AE" for w in _training(plan))


def test_grad_4_ger_ingen_lopning(monkeypatch):
    plan = _plan(monkeypatch, _with_concern(_knee(4)))
    assert not [w for w in _training(plan) if w.sport == "run"]
    assert any("Ingen löpning" in w for w in plan.warnings)


def test_rampen_star_still_sa_lange_besvaret_finns(monkeypatch):
    st = _with_concern(_knee(2))
    _plan(monkeypatch, st, dry_run=False)
    first = st["athlete_profiles"][0]["phase_state"]["ramp"]
    for row in st["planned_sessions"]:
        if row["sport"] != "Vila":
            st["training_log"].append({"user_id": row["user_id"], "date": row["date"],
                                       "sport": row["sport"], "duration_min": row["duration_min"],
                                       "source": "manual"})
    nxt = MONDAY + timedelta(weeks=1)
    plan2 = _plan(monkeypatch, st, monday=nxt, today=nxt - timedelta(days=1))
    assert plan2.engine_decisions["ramp"]["budget_minutes"] <= first["budget_minutes"]
    assert plan2.engine_decisions["ramp"]["progress_weeks"] == 0
    assert "Besvär" in plan2.engine_decisions["ramp"]["reason"]


def test_avancerad_med_axelbesvar_far_lugn_simning(monkeypatch):
    st = _with_concern({"name": "Axel", "location": "shoulder_left", "severity": 3},
                       level="advanced", hours=8, sports=("swim", "bike", "run"), race_in_weeks=20)
    plan = _plan(monkeypatch, st)
    swims = [w for w in _training(plan) if w.sport == "swim"]
    assert swims and all(w.category == "AE" for w in swims)


# ---------- MCP ser samma sak ----------


def test_coachen_ser_bedomd_paverkan_i_constraints():
    from coach.tests.test_agent_api import _client_and_store, _with_token

    c, st, aa = _client_and_store()
    st["athlete_profiles"][0]["active_concerns"] = [_knee(2)]
    st["athlete_profiles"][0]["sports"] = ["run", "bike"]
    body = c.get("/agent/constraints", headers=_with_token(st, aa)).json()
    assert "run" in str(body.get("limited_sports") or body)


def test_ui_tar_emot_auto_som_standard():
    import inspect

    import trixa_api.ui as ui

    for fn in (ui.health_add, ui.health_update):
        params = inspect.signature(fn).parameters
        assert params["impact_run"].default.default == "auto"
