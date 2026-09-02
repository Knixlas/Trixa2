"""TrainingPeaks-fynden ur kodöversynen 2026-09-02 (docs/12, avsnitt G).

G1  Dag+sport-fallbacken kunde "hitta" ett TP-pass som en annan rad redan
    ägde, radera det och ta id:t; offret matchade sin hash → "unchanged"
    för evigt → borta från klockan permanent.
G2  discipline ("bike") jämfördes mot TP-namn ("Bike") → alltid falskt →
    cancelled-rader utan lagrat id städades aldrig.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coach.integrations.trainingpeaks import workout_writer as ww  # noqa: E402
from coach.integrations.trainingpeaks.structure import SPORT_TYPE_MAP  # noqa: E402
from coach.trixa import sports  # noqa: E402

TP = [
    {"workoutId": 101, "workoutDay": "2026-09-05", "workoutTypeValueId": 2, "totalTime": None},
    {"workoutId": 102, "workoutDay": "2026-09-05", "workoutTypeValueId": 2, "totalTime": None},
]


def test_fallbacken_hoppar_over_pass_som_en_annan_rad_ager():
    # Rad A äger 101. Rad B (utan id) får inte ta 101 — den får 102.
    assert ww._find_existing_tp_id(TP, "2026-09-05", 2, owned_ids={101}) == 102
    # Utan ägarinfo (gamla anropare): första matchen, som förut.
    assert ww._find_existing_tp_id(TP, "2026-09-05", 2) == 101


def test_fallbacken_ger_none_nar_alla_ar_agda():
    assert ww._find_existing_tp_id(TP, "2026-09-05", 2, owned_ids={101, 102}) is None


def test_cancelled_stadning_slar_upp_med_tp_namn():
    """"bike" finns inte i SPORT_TYPE_MAP, "Bike" gör det (G2)."""
    assert "bike" not in SPORT_TYPE_MAP
    assert sports.tp_name("bike") in SPORT_TYPE_MAP


# ---------- G3: simreps får zonens tid ----------


def test_simrep_tid_foljer_zonen():
    from coach.integrations.trainingpeaks.mapping import build_tp_structure

    def seconds_for(zone: int) -> int:
        workout = {"discipline": "swim", "code": "T", "name": "T", "intent": "",
                   "main_set": [{"segment": "main", "type": "steady",
                                 "distance_m": 400, "zone": zone}]}
        res = build_tp_structure(workout, 30, css_sec_per_100m=120.0,
                                 threshold_pace_sec_per_km=None)
        return int(res.structure["steps"][0]["duration_seconds"])

    z1, z3, z5 = seconds_for(1), seconds_for(3), seconds_for(5)
    assert z1 > z3 > z5                      # långsammare zon → längre tid
    assert abs(z3 - 480) <= 12               # Z3 ≈ CSS-tempo (400 m @ 2:00)


# ---------- G4: en dedup för alla lager ----------


def test_veckovolymen_raknar_tp_och_strava_som_ett_pass():
    from coach.trixa.training_log import dedup_cross_source

    rows = [
        {"date": "2026-09-01", "sport": "Cykel", "duration_min": 60.0, "source": "tp"},
        {"date": "2026-09-01", "sport": "Ride", "duration_min": 60.2, "source": "strava"},
        {"date": "2026-09-01", "sport": "Cykel", "duration_min": 25.0, "source": "tp"},
        {"date": "2026-09-01", "sport": "Cykel", "duration_min": 25.0, "source": "tp"},
    ]
    kept = dedup_cross_source(rows)
    assert sum(r["duration_min"] for r in kept) == 110.0   # 60 + 25 + 25
