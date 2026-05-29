"""
Tester for zones-modulen.

Kor med:  python -m pytest coach/tests/test_zones.py -v
Eller:   python coach/tests/test_zones.py  (utan pytest)
"""
import sys
from pathlib import Path

# Lat oss kora utan att installera paketet
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from coach.engine.zones import (
    Zone,
    cycling_zones,
    running_hr_zones,
    running_pace_zones,
    swimming_zones,
    zone_for_value,
    all_zones_from_config,
)


def test_cycling_zones_basic():
    z = cycling_zones(ftp_watts=250)
    assert len(z) == 5
    assert z[0].number == 1
    assert z[0].name == "Aktiv Aterhamtning"
    # Z4 Troskel ska tacka 91-105% av 250 = 228-262 (avrundat)
    assert z[3].name == "Troskel"
    assert z[3].low == 228
    assert z[3].high == 262
    # Z5 VO2max top = 120% av 250 = 300
    assert z[4].high == 300


def test_cycling_invalid_ftp():
    try:
        cycling_zones(0)
    except ValueError:
        return
    raise AssertionError("Skulle ha kastat ValueError vid ftp=0")


def test_running_hr_zones_no_lt():
    z = running_hr_zones(threshold_hr=170)
    assert len(z) == 5
    # Z4 = 95-99% av 170 = 162-168
    assert z[3].low == 162
    assert z[3].high == 168


def test_running_hr_zones_with_lt():
    # AT=170, LT=150 (88% av AT, inom giltig range 85-94%)
    z = running_hr_zones(threshold_hr=170, lactate_threshold_hr=150)
    # Z2 ska sluta vid LT-1, Z3 ska borja vid LT
    assert z[1].high == 149   # LT-1
    assert z[2].low == 150    # LT


def test_running_hr_lt_outside_range_falls_back():
    # LT=120 ar for lagt (< 85% av 170 = 145), ska falla tillbaka
    z = running_hr_zones(threshold_hr=170, lactate_threshold_hr=120)
    z_no_lt = running_hr_zones(threshold_hr=170)
    assert z == z_no_lt


def test_running_pace_zones_z4_around_threshold():
    # AT-pace = 240 s/km (4:00/km)
    z = running_pace_zones(threshold_pace_sec_per_km=240)
    # 95% av AT-pace = 240/0.95 = 252 (langsammare)
    # 99% av AT-pace = 240/0.99 = 242 (snabbare)
    # Sa Z4 ska tacka ca 242-252
    assert 240 <= z[3].high <= 260
    assert 240 <= z[3].low <= 260


def test_swimming_zones():
    z = swimming_zones(css_sec_per_100m=90)
    assert len(z) == 5
    # Z3 Tempo = CSS exakt
    assert z[2].name == "Tempo"
    assert z[2].low == 90
    assert z[2].high == 90
    # Z5 = CSS-10 till CSS-6
    assert z[4].low == 80
    assert z[4].high == 84


def test_zone_for_value():
    z = cycling_zones(ftp_watts=250)
    found = zone_for_value(z, 230)
    assert found is not None
    assert found.number == 4  # 230 ar i troskelzonen


def test_all_zones_from_config_full():
    config = {
        "thresholds": {
            "ftp_watts": 250,
            "threshold_hr_run": 170,
            "threshold_pace_run_sec_per_km": 240,
            "css_sec_per_100m": 90,
        }
    }
    zones = all_zones_from_config(config)
    assert "cycling" in zones
    assert "running_hr" in zones
    assert "running_pace" in zones
    assert "swimming" in zones


def test_all_zones_from_config_partial():
    # Bara FTP kand
    config = {"thresholds": {"ftp_watts": 250}}
    zones = all_zones_from_config(config)
    assert "cycling" in zones
    assert "running_hr" not in zones


# ---------------------------------------------------------------------------
# Manuell test-runner om pytest inte finns
# ---------------------------------------------------------------------------
def _run_all():
    tests = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
