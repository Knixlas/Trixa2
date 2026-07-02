"""Tester för zones-modulen (dagens API: compute_zones → ZoneSet/ZoneRange).

Kör med:  py -3 -m pytest coach/tests/test_zones.py -q
"""
import sys
from pathlib import Path

import pytest

# Låt oss köra utan att installera paketet
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from coach.engine.loader import AthleteProfile
from coach.engine.zones import ZoneRange, ZoneSet, compute_zones


FULL_PROFILE = AthleteProfile(
    css_sec_per_100m=90.0,
    ftp_watts=250,
    lthr_bike_bpm=160,
    threshold_pace_sec_per_km=300.0,
    at_hr_run_bpm=170,
)


# ---------------------------------------------------------------------------
# Bike
# ---------------------------------------------------------------------------

def test_bike_watt_zones_ftp_250():
    zs = compute_zones("bike", FULL_PROFILE)
    assert zs.discipline == "bike"
    assert set(zs.zones) == {1, 2, 3, 4, 5}
    # Z4 Tröskel = 91-105% av 250
    assert zs.get(4).watts == (228, 262)
    # Z2 Endurance = 56-75% av 250
    assert zs.get(2).watts == (140, 188)
    # Z5-taket = 120% av 250
    assert zs.get(5).watts[1] == 300
    # Z1 börjar på 0
    assert zs.get(1).watts[0] == 0


def test_bike_hr_zones_lthr_160():
    zs = compute_zones("bike", FULL_PROFILE)
    # Z4 slutar exakt på LTHR (100%)
    assert zs.get(4).hr_bpm == (150, 160)
    # Z5 ligger över tröskeln
    assert zs.get(5).hr_bpm == (162, 170)


def test_bike_without_ftp_gives_hr_only():
    profile = AthleteProfile(lthr_bike_bpm=160)
    zs = compute_zones("bike", profile)
    assert zs.get(4).watts is None
    assert zs.get(4).hr_bpm == (150, 160)


def test_bike_without_any_thresholds_gives_empty_ranges():
    zs = compute_zones("bike", AthleteProfile())
    # Zonerna finns (renderaren kan visa "Z2" som etikett) men saknar målvärden
    assert set(zs.zones) == {1, 2, 3, 4, 5}
    for z in range(1, 6):
        zr = zs.get(z)
        assert zr.watts is None and zr.hr_bpm is None


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def test_run_pace_zones_threshold_300():
    zs = compute_zones("run", FULL_PROFILE)
    # Z4 = 99-104% av tröskelpace: (snabbast, långsammast) i sek/km
    assert zs.get(4).pace_sec_per_km == pytest.approx((297.0, 312.0))
    # Z5 är snabbare än tröskeln
    assert zs.get(5).pace_sec_per_km == pytest.approx((279.0, 294.0))
    # Z1 är mycket långsammare (130-150%)
    assert zs.get(1).pace_sec_per_km == pytest.approx((390.0, 450.0))
    # Tupel-ordning: snabbast (lägst sekundtal) först
    for z in range(1, 6):
        lo, hi = zs.get(z).pace_sec_per_km
        assert lo <= hi


def test_run_hr_zones_at_170():
    zs = compute_zones("run", FULL_PROFILE)
    # Z4 slutar exakt på AT (100%)
    assert zs.get(4).hr_bpm == (162, 170)
    assert zs.get(2).hr_bpm == (146, 151)
    # Run-zoner har ingen watt-data
    assert zs.get(4).watts is None


def test_run_without_pace_gives_hr_only():
    profile = AthleteProfile(at_hr_run_bpm=170)
    zs = compute_zones("run", profile)
    assert zs.get(3).pace_sec_per_km is None
    assert zs.get(3).hr_bpm is not None


def test_run_without_any_thresholds_gives_empty_ranges():
    zs = compute_zones("run", AthleteProfile())
    for z in range(1, 6):
        zr = zs.get(z)
        assert zr.pace_sec_per_km is None and zr.hr_bpm is None


# ---------------------------------------------------------------------------
# Swim
# ---------------------------------------------------------------------------

def test_swim_zones_css_90():
    zs = compute_zones("swim", FULL_PROFILE)
    assert zs.discipline == "swim"
    # Z3 = tröskelzon runt CSS
    assert zs.get(3).pace_sec_per_100m == (89.0, 91.0)
    # Z5 = sprint, snabbast
    assert zs.get(5).pace_sec_per_100m == (80.0, 84.0)
    # Z1 = recovery, långsammast
    assert zs.get(1).pace_sec_per_100m == (96.0, 100.0)
    # Sim-zoner har varken watt eller puls
    assert zs.get(3).watts is None and zs.get(3).hr_bpm is None


def test_swim_zones_are_contiguous_in_whole_seconds():
    """Regressionstest för expertgranskningens fynd 2026-07-02.

    De gamla offsetsen lämnade luckor: pace CSS+2 och CSS-2 tillhörde
    ingen zon. Gränserna ska vara heltäckande — varje zons snabba kant
    ligger exakt 1 sek under nästa långsammare zons kant.
    """
    zs = compute_zones("swim", FULL_PROFILE)
    # Snabbast → långsammast: Z5, Z4, Z3, Z2, Z1
    ordered = [zs.get(z).pace_sec_per_100m for z in (5, 4, 3, 2, 1)]
    for (_, hi), (next_lo, _) in zip(ordered, ordered[1:]):
        assert next_lo == hi + 1, (
            f"lucka mellan {hi} och {next_lo}: paces däremellan tillhör ingen zon"
        )
    # De tidigare luckorna specifikt: CSS+2 (92) och CSS-2 (88)
    assert zs.get(2).pace_sec_per_100m[0] == 92.0
    assert zs.get(4).pace_sec_per_100m[1] == 88.0


def test_swim_offsets_do_not_scale_with_css():
    """Dokumenterar känd förenkling: offsetsen är fasta sekunder.

    Zonbredderna är identiska för en snabb (CSS 75) och långsam (CSS 110)
    simmare, trots att ±5 sek/100m är relativt sett mycket mer för den
    snabba. Medveten avvägning tills procentbaserade zoner ev. införs.
    """
    fast = compute_zones("swim", AthleteProfile(css_sec_per_100m=75.0))
    slow = compute_zones("swim", AthleteProfile(css_sec_per_100m=110.0))
    for z in range(1, 6):
        f_lo, f_hi = fast.get(z).pace_sec_per_100m
        s_lo, s_hi = slow.get(z).pace_sec_per_100m
        assert (f_lo - 75.0, f_hi - 75.0) == (s_lo - 110.0, s_hi - 110.0)


def test_swim_without_css_gives_empty_zoneset():
    zs = compute_zones("swim", AthleteProfile())
    assert zs.discipline == "swim"
    assert zs.zones == {}
    assert zs.get(3) is None


# ---------------------------------------------------------------------------
# Övriga discipliner + felfall
# ---------------------------------------------------------------------------

def test_strength_and_brick_give_empty_zonesets():
    for discipline in ("strength", "brick"):
        zs = compute_zones(discipline, FULL_PROFILE)
        assert zs.discipline == discipline
        assert zs.zones == {}


def test_unknown_discipline_raises():
    with pytest.raises(ValueError):
        compute_zones("rowing", FULL_PROFILE)
