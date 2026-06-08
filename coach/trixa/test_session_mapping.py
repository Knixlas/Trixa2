"""Tester för fritext→passbankskod-mappningen och struktureringen.

Rena transformer mot den riktiga passbanken + regeltabellen. Ingen DB, inget nät.
Kör via projektets test-runner (pytest ej installerat i miljön).
"""

from __future__ import annotations

from coach.engine.loader import load_workouts
from coach.trixa.session_mapping import load_session_mapping, resolve_session
from coach.trixa.structure_sessions import structure_rows

_POOL = {w["code"]: w for w in load_workouts()}
_RULES = load_session_mapping()


def _r(sport, title, dur):
    return resolve_session(sport, title, dur, _POOL, _RULES)[0]


def test_resolve_known_nils_sessions():
    # Reproducerar exakt de manuella valen för vecka 8-14 juni.
    assert _r("Cykel", "Cykel Z2 - kontrollerad start", 60) == "AE2_bike_02"
    assert _r("Cykel", "Cykel Z2", 85) == "AE2_bike_template"
    assert _r("Cykel", "Långpass cykel 2h - veckans ankare", 120) == "AE2_bike_template"
    assert _r("Löpning", "Löpning lätt (villkorad)", 30) == "AE1_run_template"
    assert _r("Löpning", "Löpning lätt (villkorad)", 40) == "AE1_run_template"
    assert _r("Sim", "Teknik + aerobt", 40) == "AE1_swim_01"


def test_keyword_priority_specific_over_generic():
    assert _r("Cykel", "Tröskel 2x20", 75) in ("ME1_bike_01", "ME4_bike_01", "ME1_bike_02")
    assert _r("Cykel", "Crisscross block", 75) in ("ME3_bike_01", "ME3_bike_02")
    assert _r("Löpning", "Backintervall 6x90", 60) in ("MF3_run_01", "MF1_run_01", "ME2_run_01")
    assert _r("Sim", "Teknikpass + drillar", 40) == "AE1_swim_01"


def test_duration_fit_within_rule():
    # VO2 bike: 30/30 = AC1_bike_02 (40-60), längre VO2 = AC1_bike_01 (55-80).
    assert _r("Cykel", "VO2 30/30", 50) == "AC1_bike_02"
    assert _r("Cykel", "VO2-intervaller", 70) == "AC1_bike_01"


def test_no_match_returns_none():
    assert _r("Cykel", "qwerty zzz", 60) is None
    assert resolve_session("Vila", "Vila", 0, _POOL, _RULES) == (None, None)
    assert resolve_session("Styrka", "Benpass gym", 45, _POOL, _RULES) == (None, None)


def test_structure_rows_respects_code_and_escalates():
    rows = [
        {"id": 1, "date": "2026-06-09", "sport": "Sim", "title": "valfritt",
         "workout_code": "AE1_swim_01", "duration_min": 40, "steps": None},
        {"id": 2, "date": "2026-06-10", "sport": "Cykel", "title": "Cykel Z2",
         "workout_code": None, "duration_min": 85, "steps": None},
        {"id": 3, "date": "2026-06-11", "sport": "Cykel", "title": "qwerty",
         "workout_code": None, "duration_min": 60, "steps": None},
        {"id": 4, "date": "2026-06-12", "sport": "Vila", "title": "Vila",
         "workout_code": None, "duration_min": 0, "steps": None},
        {"id": 5, "date": "2026-06-13", "sport": "Cykel", "title": "Cykel Z2",
         "workout_code": None, "duration_min": 60, "steps": [{"segment": "main"}]},
    ]
    res = structure_rows(rows, _POOL, _RULES)
    upd = {u["id"]: u for u in res.to_update}
    assert upd[1]["code"] == "AE1_swim_01" and upd[1]["source"] == "workout_code"
    assert upd[2]["code"] == "AE2_bike_template" and upd[2]["source"] == "rule"
    assert len(upd[1]["steps"]) > 0 and len(upd[2]["steps"]) > 0
    assert [u["id"] for u in res.unmatched] == [3]
    skipped_ids = {s["id"] for s in res.skipped}
    assert 4 in skipped_ids and 5 in skipped_ids   # vila + redan-steps


def test_structured_steps_map_cleanly_to_tp():
    # Det strukturerade resultatet ska gå att bygga TP-struktur av utan varningar.
    from coach.integrations.trainingpeaks.mapping import build_tp_structure
    from coach.integrations.trainingpeaks.structure import compute_if_tss
    rows = [{"id": 1, "date": "2026-06-13", "sport": "Cykel",
             "title": "Långpass cykel 2h", "workout_code": None,
             "duration_min": 120, "steps": None}]
    u = structure_rows(rows, _POOL, _RULES).to_update[0]
    res = build_tp_structure(
        {"discipline": "bike", "main_set": u["steps"], "code": u["code"]}, 120)
    assert res.warnings == []
    _, _, total_s = compute_if_tss(res.structure)
    assert total_s == 120 * 60
