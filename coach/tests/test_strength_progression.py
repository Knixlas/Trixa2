"""Tester för den autoreglerade lastprogressionen.

Passbanken beskrev modellen ("progredierar nästa gång samma RIR nås vid lägre
ansträngning — detta ÄR autoreglering", strength_MS.yaml) men ingen kod bar
den: loggen tog emot vikt och ansträngning, och ingenting läste dem tillbaka.

Det som testas är att avbockningen faktiskt styr nästa pass, och lika viktigt:
att den styr det FÖRSIKTIGT. En för stor ökning kostar ett pass eller en axel;
en missad ökning kostar en vecka.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coach.trixa.exercise_plan import exercises_from_steps  # noqa: E402
from coach.trixa.strength_progression import (  # noqa: E402
    apply_suggestions,
    rep_span,
    round_load,
    suggest_next,
    suggestions_by_name,
)

# Ett MS-block: spannet 3-6 reps, tung last (strength.yaml::protocol_parameters).
SQUAT = {
    "code": "back_squat", "name": "Knäböj", "sets": 3, "reps": 5,
    "reps_min": 3, "reps_max": 6, "rir": 2, "load": "80% 1RM",
}
PUSHUP = {
    "code": "pushup", "name": "Armhävningar", "sets": 2, "reps": 12,
    "reps_min": 12, "reps_max": 15, "rir": 4, "load": "kroppsvikt",
}


def _log(date: str, weight=60.0, reps=5, effort=2, sets=3, code="back_squat"):
    return {
        "session_date": date, "exercise_name": "Knäböj", "exercise_code": code,
        "sets": sets, "reps": reps, "weight_from": weight, "effort": effort,
    }


# ---------- viktstegen ----------


def test_round_load_traffar_vikter_som_finns_i_gymmet():
    """41,3 kg går inte att lasta. Förslaget måste vara körbart."""
    assert round_load(41.3) == 42.5     # skivstång: 2,5 kg
    assert round_load(23.4) == 23.0     # hantelrack: 1 kg
    assert round_load(6.2) == 6.0       # småhantlar: 0,5 kg
    assert round_load(0) == 0.0


def test_okning_ar_aldrig_mindre_an_ett_viktsteg():
    """2,5 % av 40 kg är 1 kg — men stången tar bara 2,5 kg-steg.

    Utan golvet avrundas ökningen bort och adepten kör samma vikt för alltid
    medan appen påstår att den progredierar.
    """
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=40.0, reps=6, effort=2)])
    assert out.weight == 42.5
    assert out.trend == "up"


# ---------- dubbel progression ----------


def test_latt_under_taket_ger_fler_reps_inte_mer_vikt():
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=3, effort=1)])
    assert out.weight == 60.0
    assert out.reps == 5              # 3 + 2, inom spannet
    assert "60 kg" in out.reason


def test_latt_vid_taket_vaxlar_reps_mot_vikt():
    """Hela poängen med spannet: vid taket byts reps mot kilon, och reps
    börjar om på golvet."""
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=6, effort=1)])
    assert out.weight == 62.5         # +5 %, avrundat till skivsteg
    assert out.reps == 3              # tillbaka till spannets golv
    assert out.trend == "up"


def test_lagom_okar_ett_rep_i_taget():
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=4, effort=2)])
    assert out.weight == 60.0
    assert out.reps == 5


def test_tungt_men_klarade_haller_kvar_vikten():
    """Att öka på ett pass som redan kändes tungt är hur folk skadar sig."""
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=5, effort=3)])
    assert out.weight == 60.0
    assert out.reps == 5
    assert out.trend == "hold"


def test_for_tungt_backar_vikten():
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=4, effort=4)])
    assert out.weight == 57.5
    assert out.reps == 3
    assert out.trend == "down"


# ---------- när loggen och kroppen säger olika ----------


def test_missade_reps_slar_ut_angiven_anstrangning():
    """Adepten kryssade 'lagom' men klarade två reps av spannets tre.

    Kryssrutan är en åsikt; reps är ett mätvärde. Mätvärdet vinner, annars
    föreslår motorn en ökning på en vikt som inte gick att lyfta.
    """
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=60.0, reps=2, effort=2)])
    assert out.trend == "down"
    assert out.weight == 57.5
    assert out.warnings and "för tungt" in out.warnings[0]


def test_stagnation_utloser_deload():
    """Tre pass på samma vikt utan att den lättat är inte tålamod."""
    history = [
        _log("2026-08-25", weight=60.0, effort=3),
        _log("2026-08-18", weight=60.0, effort=3),
        _log("2026-08-11", weight=60.0, effort=4),
    ]
    out = suggest_next(SQUAT, history)
    assert out.trend == "deload"
    assert out.weight == 55.0         # -10 %
    assert "backa" in out.reason


def test_tva_pass_pa_samma_vikt_ar_inte_stagnation():
    history = [
        _log("2026-08-25", weight=60.0, effort=3),
        _log("2026-08-18", weight=60.0, effort=3),
    ]
    assert suggest_next(SQUAT, history).trend == "hold"


def test_overhoppat_pass_sager_inget_om_lasten():
    """Ett hoppat pass får inte nolla historiken — vikten före gäller."""
    history = [
        _log("2026-08-25", weight=None, reps=None, effort=-1),
        _log("2026-08-18", weight=60.0, reps=6, effort=1),
    ]
    out = suggest_next(SQUAT, history)
    assert out.weight == 62.5
    assert out.previous["session_date"] == "2026-08-18"


def test_forsta_gangen_gissar_ingen_vikt():
    """Att hitta på ett startvärde vore att låtsas veta. Be om det i stället."""
    out = suggest_next(SQUAT, [])
    assert out.trend == "new"
    assert out.weight is None
    assert out.reps == 5              # planerat rep-tal behålls
    assert "2 reps i tanken" in out.reason


def test_bara_overhoppade_rader_raknas_som_forsta_gangen():
    out = suggest_next(SQUAT, [_log("2026-08-25", weight=None, effort=-1)])
    assert out.trend == "new"
    assert "överhoppad" in out.reason


# ---------- kroppsvikt ----------


def test_kroppsvikt_progredierar_i_reps():
    out = suggest_next(PUSHUP, [{
        "session_date": "2026-08-25", "exercise_name": "Armhävningar",
        "exercise_code": "pushup", "sets": 2, "reps": 12, "weight_from": None,
        "effort": 1,
    }])
    assert out.weight is None
    assert out.reps == 14
    assert out.trend == "up"


def test_kroppsvikt_vid_taket_foreslar_tyngre_variant():
    out = suggest_next(PUSHUP, [{
        "session_date": "2026-08-25", "exercise_name": "Armhävningar",
        "exercise_code": "pushup", "sets": 2, "reps": 15, "weight_from": None,
        "effort": 1,
    }])
    assert out.reps == 12
    assert "tyngre variant" in out.reason


# ---------- spannet ----------


def test_rep_span_faller_tillbaka_pa_planerat_tal():
    """Veckor lagda innan spannet fanns ska inte krascha progressionen."""
    assert rep_span({"reps": 8}) == (8, 10)
    assert rep_span({}) == (8, 10)
    assert rep_span({"reps_min": 3, "reps_max": 6}) == (3, 6)


def test_spannet_foljer_med_fran_passets_mall():
    """parameters.reps.range → reps_min/reps_max på varje övning."""
    steps = [{
        "segment": "strength_block", "order": 1, "exercise": "back_squat",
        "prescription": {"sets": 3, "reps": 5, "rir": 2}, "load_pct": "80% 1RM",
    }]
    ex = exercises_from_steps(steps, None, {"default": 5, "range": [3, 6]})[0]
    assert (ex["reps_min"], ex["reps_max"]) == (3, 6)


def test_spannet_i_steget_vinner_over_mallens():
    steps = [{
        "segment": "strength_block", "exercise": "back_squat",
        "prescription": {"reps": {"default": 10, "range": [8, 12]}},
    }]
    ex = exercises_from_steps(steps, None, {"range": [3, 6]})[0]
    assert (ex["reps_min"], ex["reps_max"]) == (8, 12)


# ---------- listan som formuläret får ----------


def test_apply_suggestions_fyller_formularet_utan_att_mutera_planen():
    planned = [dict(SQUAT)]
    original = dict(SQUAT)
    out = apply_suggestions(planned, [_log("2026-08-25", weight=60.0, reps=6, effort=1)])
    assert planned[0] == original          # planen är orörd
    assert out[0]["weight_from"] == 62.5   # formuläret är förifyllt
    assert out[0]["reps"] == 3
    assert out[0]["suggestion"]["trend"] == "up"


def test_historik_matchas_pa_kod_nar_namnet_skrivits_om():
    """Byter katalogen 'Knäböj' mot 'Knäböj med skivstång' ska historiken följa
    med — annars börjar progressionen om från noll vid en ren namnändring."""
    renamed = dict(SQUAT, name="Knäböj med skivstång")
    out = apply_suggestions(
        [renamed], [_log("2026-08-25", weight=60.0, reps=6, effort=1)]
    )
    assert out[0]["weight_from"] == 62.5


# ---------- coachens reps är en föreskrift ----------


def test_coachens_reps_star_kvar_men_vikten_foljer_anstrangningen():
    """"3×10, djupet ändras först när svullnaden varit tyst två veckor" får
    inte bli 3×12 för att förra passet kändes lätt. Vikten är det coachen
    inte kan se — den följer fortfarande."""
    planned = [{"name": "Knäböj", "code": "back_squat", "sets": 3, "reps": 10,
                "reps_min": 3, "reps_max": 6}]
    logs = [_log("2026-08-25", weight=60.0, reps=6, effort=1)]
    out = apply_suggestions(planned, logs, coach_prescribed=True)
    assert out[0]["reps"] == 10                      # inte spannets golv
    assert out[0]["weight_from"] == 62.5             # vikten rör sig ändå
    assert "Reps enligt coachens pass: 10" in out[0]["suggestion"]["reason"]


def test_genererat_pass_far_reps_flyttade():
    planned = [{"name": "Knäböj", "code": "back_squat", "sets": 3, "reps": 5,
                "reps_min": 3, "reps_max": 6}]
    logs = [_log("2026-08-25", weight=60.0, reps=6, effort=1)]
    out = apply_suggestions(planned, logs, coach_prescribed=False)
    assert out[0]["reps"] == 3


# ---------- övningar utan rep-tal ----------


def test_ovning_utan_reptal_far_inget_pahittat():
    """Dödhäng "till nära utmattning" har inget rep-tal. Att hitta på 8 och
    räkna progression på det vore att föreslå åtta dödhäng."""
    s = suggest_next({"name": "Dödhäng", "sets": 4, "load": "kroppsvikt"}, [{
        "session_date": "2026-09-07", "exercise_name": "Dödhäng",
        "sets": 4, "reps": None, "weight_from": None, "effort": 1,
    }])
    assert s.reps is None
    assert s.weight is None
    assert "Inget rep-tal" in s.reason and "Öka tid" in s.reason


def test_ovning_utan_reptal_men_med_vikt_foljer_vikten():
    """Farmer's walk: sträcka i noten, men vikten är mätbar och progredierar."""
    s = suggest_next({"name": "Farmer's walk", "sets": 3, "load": "hantlar"}, [{
        "session_date": "2026-09-07", "exercise_name": "Farmer's walk",
        "sets": 3, "reps": None, "weight_from": 24.0, "effort": 1,
    }])
    assert s.reps is None
    assert s.weight == 25.0          # 24 × 1,05 = 25,2 → närmaste hantelsteg
    assert s.trend == "up"


def test_forsta_gangen_utan_reptal_hittar_inte_pa_reps():
    s = suggest_next({"name": "Dödhäng", "sets": 4, "load": "kroppsvikt"}, [])
    assert s.reps is None


# ---------- övningar utanför planen ----------


def test_suggestions_by_name_tacker_pass_som_bar_ovningarna_som_prosa():
    """Ett coach-skrivet pass kan ha övningarna i löptext utan strukturerad
    lista. Adepten har ändå en historik, och passets form är inte hens val."""
    out = suggestions_by_name([_log("2026-08-25", weight=60.0, reps=6, effort=1)])
    assert "knäböj" in out                 # uppslag på det adepten skriver
    assert out["knäböj"]["weight"] == 62.5
    assert out["knäböj"]["code"] == "back_squat"
    assert out["knäböj"]["trend"] == "up"


def test_planlos_ovning_flyttar_vikten_i_stallet_for_att_klattra_i_reps():
    """Regression: ett repspann som följde med senast loggade reps flyttade
    taket varje gång. Reps klättrade i all evighet och vikten steg aldrig —
    alltså ingen progression alls, bara längre set.

    Utan protokoll finns inget spann att växla inom, så reps låses vid det
    adepten körde och hela progressionen sitter i vikten.
    """
    first = suggestions_by_name([_log("2026-08-25", weight=60.0, reps=6, effort=1)])
    assert first["knäböj"]["weight"] == 62.5
    assert first["knäböj"]["reps"] == 6           # oförändrat, inte 8
    # Inget spann föreskrevs, så tala inte om ett tak adepten aldrig sett.
    assert "Taket" not in first["knäböj"]["reason"]
    assert "samma 6 reps" in first["knäböj"]["reason"]

    later = suggestions_by_name([_log("2026-09-01", weight=62.5, reps=6, effort=1)])
    assert later["knäböj"]["weight"] == 65.0      # 62,5 × 1,05 → närmaste skivsteg


def test_planlos_kroppsviktsovning_progredierar_fortfarande_i_reps():
    """Utan vikt finns bara reps att skruva på."""
    out = suggestions_by_name([{
        "session_date": "2026-08-25", "exercise_name": "Chins",
        "sets": 3, "reps": 8, "weight_from": None, "effort": 1,
    }])
    assert out["chins"]["weight"] is None
    assert out["chins"]["reps"] == 10


def test_suggestions_by_name_hoppar_over_ovningar_utan_utfort_pass():
    out = suggestions_by_name([_log("2026-08-25", weight=None, effort=-1)])
    assert out == {}


def test_okand_ovning_far_inte_arva_annan_ovnings_vikt():
    out = apply_suggestions(
        [dict(SQUAT, code="deadlift", name="Marklyft")],
        [_log("2026-08-25", weight=60.0, reps=6, effort=1)],
    )
    assert out[0]["suggestion"]["trend"] == "new"
    assert out[0].get("weight_from") is None
