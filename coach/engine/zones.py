"""Beräkna träningszoner per disciplin från adept-profil.

Varje disciplin har sitt eget zon-system:
- Sim: pace-zoner relativt CSS (Critical Swim Speed, sek/100m)
- Bike: parallella zoner för watt (relativt FTP) + puls (relativt LTHR)
- Run: parallella zoner för pace (relativt threshold-pace) + puls (relativt AT)

Bike och run returnerar **båda** målvärden samtidigt. Watt/pace är primärt,
puls sekundärt. Vid avvikelse: tröskel+ → watt/pace vinner, aerobt → puls
kan signalera värme/dehydrering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .loader import AthleteProfile


Discipline = Literal["swim", "bike", "run"]


@dataclass
class ZoneRange:
    """En zon med möjliga parallella målvärden."""

    pace_sec_per_100m: tuple[float, float] | None = None  # sim
    pace_sec_per_km: tuple[float, float] | None = None    # run
    watts: tuple[int, int] | None = None                  # bike
    hr_bpm: tuple[int, int] | None = None                 # bike, run


@dataclass
class ZoneSet:
    """Komplett zonset för en disciplin, indexerat 1-5."""

    discipline: Discipline
    zones: dict[int, ZoneRange] = field(default_factory=dict)

    def get(self, zone: int) -> ZoneRange | None:
        return self.zones.get(zone)


# ---------- Sim ----------

def _swim_zones(css_sec_per_100m: float) -> ZoneSet:
    """Sim-zoner som offsets från CSS.

    Offset-mönstret är välanvänt i tritränarlitteratur. Z1 = recovery,
    Z3 = CSS-pace (tröskel), Z5 = sprint.
    """
    offsets = {
        1: (6, 10),     # Z1: CSS+6 till CSS+10
        2: (3, 5),
        3: (-1, 1),
        4: (-5, -3),
        5: (-10, -6),
    }
    zones: dict[int, ZoneRange] = {}
    for z, (lo_off, hi_off) in offsets.items():
        zones[z] = ZoneRange(
            pace_sec_per_100m=(css_sec_per_100m + lo_off, css_sec_per_100m + hi_off)
        )
    return ZoneSet(discipline="swim", zones=zones)


# ---------- Bike ----------

# FTP-baserade watt-zoner (Coggan-modellen, något justerad)
_BIKE_WATT_FRACTIONS = {
    1: (0.00, 0.55),   # Recovery
    2: (0.56, 0.75),   # Endurance
    3: (0.76, 0.90),   # Tempo
    4: (0.91, 1.05),   # Threshold
    5: (1.06, 1.20),   # VO2 (förkortad — Z6/Z7 finns men inte här)
}

# LTHR-baserade pulszoner för cykel (Friel-modellen)
_BIKE_HR_FRACTIONS = {
    1: (0.00, 0.81),
    2: (0.82, 0.88),
    3: (0.89, 0.93),
    4: (0.94, 1.00),
    5: (1.01, 1.06),
}


def _bike_zones(profile: AthleteProfile) -> ZoneSet:
    zones: dict[int, ZoneRange] = {}
    for z in range(1, 6):
        watts = None
        if profile.ftp_watts:
            lo_f, hi_f = _BIKE_WATT_FRACTIONS[z]
            watts = (round(profile.ftp_watts * lo_f), round(profile.ftp_watts * hi_f))
        hr = None
        if profile.lthr_bike_bpm:
            lo_f, hi_f = _BIKE_HR_FRACTIONS[z]
            hr = (round(profile.lthr_bike_bpm * lo_f), round(profile.lthr_bike_bpm * hi_f))
        zones[z] = ZoneRange(watts=watts, hr_bpm=hr)
    return ZoneSet(discipline="bike", zones=zones)


# ---------- Run ----------

# Pace-zoner som procent av threshold-pace (Daniels/Friel-hybrid).
# Långsammare = högre värde i sek/km. Z1 är 130-150% av threshold (mycket långsammare).
_RUN_PACE_FRACTIONS = {
    1: (1.30, 1.50),
    2: (1.15, 1.29),
    3: (1.05, 1.14),
    4: (0.99, 1.04),
    5: (0.93, 0.98),
}

# AT-baserade pulszoner för löpning
_RUN_HR_FRACTIONS = {
    1: (0.00, 0.85),
    2: (0.86, 0.89),
    3: (0.90, 0.94),
    4: (0.95, 1.00),
    5: (1.01, 1.06),
}


def _run_zones(profile: AthleteProfile) -> ZoneSet:
    zones: dict[int, ZoneRange] = {}
    for z in range(1, 6):
        pace = None
        if profile.threshold_pace_sec_per_km:
            lo_f, hi_f = _RUN_PACE_FRACTIONS[z]
            # OBS: lo_f < hi_f i värdet men *snabbare* pace = lägre sekundtal
            # Så lo=snabb, hi=långsam blir motsatt här — pace_sec_per_km
            # presenteras som (snabbast, långsammast).
            pace = (
                profile.threshold_pace_sec_per_km * lo_f,
                profile.threshold_pace_sec_per_km * hi_f,
            )
        hr = None
        if profile.at_hr_run_bpm:
            lo_f, hi_f = _RUN_HR_FRACTIONS[z]
            hr = (round(profile.at_hr_run_bpm * lo_f), round(profile.at_hr_run_bpm * hi_f))
        zones[z] = ZoneRange(pace_sec_per_km=pace, hr_bpm=hr)
    return ZoneSet(discipline="run", zones=zones)


# ---------- Publik dispatcher ----------


def compute_zones(discipline: Discipline, profile: AthleteProfile) -> ZoneSet:
    """Returnera ZoneSet för disciplinen baserat på adept-profilen.

    Saknade testvärden ger zoner utan konkreta målvärden — renderaren
    faller då tillbaka till "Z2" som etikett utan watt/pace/puls-spann.
    """
    if discipline == "swim":
        if profile.css_sec_per_100m is None:
            return ZoneSet(discipline="swim")
        return _swim_zones(profile.css_sec_per_100m)
    if discipline == "bike":
        return _bike_zones(profile)
    if discipline == "run":
        return _run_zones(profile)
    raise ValueError(f"Okänd disciplin: {discipline}")
