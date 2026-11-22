"""Passtexten adepten ser: inget byggarspråk, zoner i känsla (docs/13).

En nybörjare med Midnattsloppet som mål fick metarader med passkoder,
"Parameteriserat pass — duration 30–70 min (default 50)", tränarnoter
skrivna till utvecklaren och "10 min @ Z1" utan förklaring.
"""

from __future__ import annotations

from coach.engine.loader import AthleteProfile, load_workouts
from coach.engine.renderer import render_workout
from coach.engine.templates import resolve_template


def _pass(code: str) -> dict:
    pool = {w["code"]: w for w in load_workouts()}
    return resolve_template(pool[code])


def test_adepten_ser_inte_passkod_mallspann_eller_tranarnoter():
    md = render_workout(_pass("SS2_run_template"), AthleteProfile())
    assert "SS2_run_template" not in md
    assert "faser:" not in md
    assert "Parameteriserat" not in md
    assert "Tränarens noter" not in md
    assert "Renderaren" not in md


def test_coachen_ser_allt():
    md = render_workout(_pass("SS2_run_template"), AthleteProfile(), for_coach=True)
    assert "SS2_run_template" in md
    assert "Parameteriserat" in md
    assert "Tränarens noter" in md


def test_zoner_utan_testvarden_forklaras_i_kansla():
    md = render_workout(_pass("AE2_run_template"), AthleteProfile())
    assert "@" not in md
    assert "Z2 – lugnt, du kan prata i hela meningar" in md
    assert "Z1 – mycket lugnt" in md


def test_hela_passbanken_renderas_utan_fackord():
    """Allt adepten ser, renderarens egna ord inräknade (utrustning, trampfrekvens …)."""
    import re

    from coach.engine.loader import load_drills, load_strength_exercises
    from coach.tests.test_passbank_sprak import _FORBIDDEN, _PAREN_ONLY, _outside_parens

    drills = {d["code"]: d for d in load_drills()}
    exercises = {e["code"]: e for e in load_strength_exercises()}
    hits = []
    for w in load_workouts():
        md = render_workout(resolve_template(w), AthleteProfile(), drills, exercises)
        for pat in _FORBIDDEN:
            for m in re.findall(pat, md, flags=re.IGNORECASE if pat.islower() else 0):
                hits.append(f"{w['code']}: {m!r}")
        for pat in _PAREN_ONLY:
            for m in re.findall(pat, _outside_parens(md)):
                hits.append(f"{w['code']}: {m!r} utanför parentes")
        if re.search(r"\b[a-z]+_[a-z_]+\b", md):
            hits.append(f"{w['code']}: kod med understreck {re.search(r'[a-z]+_[a-z_]+', md).group()!r}")
    assert not hits, f"{len(hits)} träffar:\n" + "\n".join(hits[:40])


def test_zoner_med_testvarden_visar_puls():
    profile = AthleteProfile(at_hr_run_bpm=170, max_hr_bpm=190)
    md = render_workout(_pass("AE2_run_template"), profile)
    assert "bpm" in md


def test_loggar_under_gamla_ovningsnamn_hittas_efter_namnbytet():
    """Tio övningar fick svenska namn; loggar utan kod bär bara det gamla."""
    from coach.engine.loader import load_strength_exercises
    from coach.trixa.strength_progression import (
        _FORMER_CATALOG_NAMES, index_history, lookup_history,
    )

    catalog = {e["code"]: e["name"] for e in load_strength_exercises()}
    for old_name, code in _FORMER_CATALOG_NAMES.items():
        assert code in catalog, code
        idx = index_history([{"exercise_name": old_name.title(), "session_date": "2026-04-01"}])
        assert lookup_history({"code": code, "name": catalog[code]}, idx), old_name
