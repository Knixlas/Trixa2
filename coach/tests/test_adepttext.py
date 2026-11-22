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


def test_zoner_med_testvarden_visar_puls():
    profile = AthleteProfile(at_hr_run_bpm=170, max_hr_bpm=190)
    md = render_workout(_pass("AE2_run_template"), profile)
    assert "bpm" in md
