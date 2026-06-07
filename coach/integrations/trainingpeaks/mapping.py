"""Passbank `main_set` → TrainingPeaks strukturformat.

Producerar den *förenklade* strukturen som TP-skapar-pass förväntar sig
(samma format som MCP:n bygger, se docs/06 §7):

    {
      "primaryIntensityMetric": "percentOfFtp" | "percentOfThresholdHr"
                                | "percentOfThresholdPace",
      "steps": [SimpleStep | {type:"repetition", reps, steps:[...]}]
    }
    SimpleStep = {name, duration_seconds, intensity_min, intensity_max,
                  intensityClass: warmUp|active|rest|coolDown}

Zon→intensitet återanvänder fraktionerna i `coach/engine/zones.py`
(källa-sanning), så strukturen följer samma zonmodell som renderaren:
- **bike** → `percentOfFtp` (watt primärt), direkt ur FTP-fraktionerna
- **run**  → `percentOfThresholdPace` (fart-% = 100/tid-fraktion)
- **swim** → `percentOfThresholdPace` (approx-band kring CSS; förfinas mot CSS)

Begränsningar i v1 (returneras som `warnings`, inte tyst tappade):
- `effort_descriptor`-segment (crisscross/over-under) saknar enskild zon →
  representeras som ett block i mitten av angiven zon; detaljen bär i texten.
- swim distans→tid kräver CSS; utan CSS hoppas distansbaserade steg över.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...engine.zones import _BIKE_WATT_FRACTIONS, _RUN_PACE_FRACTIONS

# Disciplin → TP-sportnamn (matchar SPORT_TYPE_MAP i workout_writer/MCP)
SPORT_NAME = {"swim": "Swim", "bike": "Bike", "run": "Run",
              "brick": "Brick", "strength": "Strength"}

# Disciplin → primär intensitetsmetrik i TP-strukturen
INTENSITY_METRIC = {
    "bike": "percentOfFtp",
    "run": "percentOfThresholdPace",
    "swim": "percentOfThresholdPace",
}

# Swim fart-%-band kring CSS (CSS=100%). Approximation av offset-modellen i
# zones.py tills vi konverterar mot adeptens faktiska CSS.
_SWIM_SPEED_PCT = {
    1: (78.0, 86.0),
    2: (88.0, 94.0),
    3: (97.0, 101.0),
    4: (103.0, 108.0),
    5: (110.0, 120.0),
}

# Segment som räknas som arbete (får sin zons intensitet)
_WORK_SEGMENTS = {"main", "sprint", "pull", "kick", "drills", "continuous"}


@dataclass
class TPStructureResult:
    sport: str
    structure: dict[str, Any]
    total_duration_min: float
    warnings: list[str] = field(default_factory=list)


def _intensity_pct(discipline: str, zone: int) -> tuple[float, float]:
    """Zon (1-5) → (intensity_min, intensity_max) i procent för disciplinens metrik."""
    z = max(1, min(5, int(zone)))
    if discipline == "bike":
        lo_f, hi_f = _BIKE_WATT_FRACTIONS[z]
        return (round(lo_f * 100, 1), round(hi_f * 100, 1))
    if discipline == "run":
        # _RUN_PACE_FRACTIONS är tid-fraktioner (1.30 = 30% långsammare).
        # TP percentOfThresholdPace är fart-% → 100/tid. Snabbare = högre %.
        lo_t, hi_t = _RUN_PACE_FRACTIONS[z]   # lo_t=snabbast(tid), hi_t=långsammast
        return (round(100.0 / hi_t, 1), round(100.0 / lo_t, 1))
    if discipline == "swim":
        return _SWIM_SPEED_PCT[z]
    # fallback (brick/strength): neutral band
    return (60.0, 75.0)


def _seg_zone(seg: dict) -> int:
    """Hämta segmentets zon. zones_per_set hanteras separat; default Z2."""
    z = seg.get("zone")
    if z is None:
        zps = seg.get("zones_per_set")
        if isinstance(zps, list) and zps:
            return int(zps[0])
        return 2
    return int(z)


def _resolve_sets(value: Any) -> int:
    """sets: int eller {default, range} → konkret antal (default-värdet).

    Tål ouppslagna mall-placeholders (t.ex. '{sets_per_leg}') och annat icke-
    numeriskt → faller tillbaka på 1 set i stället för att krascha hela passet.
    """
    if isinstance(value, dict):
        try:
            return int(value.get("default", 1))
        except (TypeError, ValueError):
            return 1
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _step(name: str, seconds: int, intensity: tuple[float, float],
          klass: str, cadence: tuple[int, int] | None = None) -> dict:
    step: dict[str, Any] = {
        "name": name,
        "duration_seconds": int(seconds),
        "intensity_min": intensity[0],
        "intensity_max": intensity[1],
        "intensityClass": klass,
    }
    if cadence:
        step["cadence_min"], step["cadence_max"] = cadence
    return step


def build_tp_structure(
    workout: dict,
    total_duration_min: float,
    css_sec_per_100m: float | None = None,
    threshold_pace_sec_per_km: float | None = None,
) -> TPStructureResult:
    """Bygg TP-strukturen för ett passbank-pass.

    Args:
        workout: parsad YAML-post (en post ur t.ex. bike_ME.yaml).
        total_duration_min: konkret total efter parametrisering (från planner).
        css_sec_per_100m: adeptens CSS — krävs för swim distans→tid.
        threshold_pace_sec_per_km: adeptens tröskelfart — krävs för run distans→tid.
    """
    discipline = workout.get("discipline", "bike")
    warnings: list[str] = []
    total_seconds = int(total_duration_min * 60)
    metric = INTENSITY_METRIC.get(discipline, "percentOfFtp")

    steps: list[dict] = []

    for seg in workout.get("main_set", []):
        seg_type = seg.get("segment", "main")
        zone = _seg_zone(seg)
        cadence = None
        rpm = seg.get("cadence_rpm")
        if isinstance(rpm, list) and len(rpm) == 2:
            cadence = (int(rpm[0]), int(rpm[1]))

        # intensitetsklass
        if seg_type == "warmup":
            klass = "warmUp"
        elif seg_type == "cooldown":
            klass = "coolDown"
        else:
            klass = "active"

        # crisscross/over-under utan enskild zon
        if seg.get("effort_descriptor") and seg.get("zone") is None:
            warnings.append(
                f"{workout.get('code','?')}: '{seg_type}' har effort_descriptor "
                "utan zon — approximerat till Z4-block, detaljen bär i texten."
            )
            zone = 4

        # --- segmentets tidslängd ---
        sets = _resolve_sets(seg.get("sets")) if "sets" in seg else 1
        per_rep_seconds = None
        if seg.get("duration_min") is not None:
            per_rep_seconds = int(float(seg["duration_min"]) * 60)
        elif seg.get("duration_pct") is not None:
            per_rep_seconds = int(float(seg["duration_pct"]) * total_seconds)
        elif seg.get("distance_m") is not None:
            # tids-uppskattning kräver pace/CSS — annars hoppa
            if discipline == "swim" and css_sec_per_100m:
                per_rep_seconds = int(seg["distance_m"] / 100.0 * css_sec_per_100m)
            elif discipline == "run" and threshold_pace_sec_per_km:
                lo_t, hi_t = _RUN_PACE_FRACTIONS[max(1, min(5, zone))]
                pace_factor = (lo_t + hi_t) / 2.0   # tid-fraktion av tröskelfart
                per_rep_seconds = int(
                    seg["distance_m"] / 1000.0 * threshold_pace_sec_per_km * pace_factor
                )
            else:
                warnings.append(
                    f"{workout.get('code','?')}: distansbaserat '{seg_type}' "
                    "utan pace/CSS — hoppat över i strukturen."
                )
                continue

        if not per_rep_seconds or per_rep_seconds <= 0:
            continue

        intensity = _intensity_pct(discipline, zone)
        rest_sec = int(seg.get("rest_sec") or 0)

        if sets > 1:
            inner = [_step(seg_type, per_rep_seconds, intensity, klass, cadence)]
            if rest_sec > 0:
                inner.append(_step("Vila", rest_sec, _intensity_pct(discipline, 1), "rest"))
            steps.append({"type": "repetition", "name": seg.get("description") or seg_type,
                          "reps": sets, "steps": inner})
        else:
            steps.append(_step(seg_type, per_rep_seconds, intensity, klass, cadence))

    if not steps:
        warnings.append(f"{workout.get('code','?')}: inga byggbara steg.")

    structure = {"primaryIntensityMetric": metric, "steps": steps}
    return TPStructureResult(
        sport=SPORT_NAME.get(discipline, "Other"),
        structure=structure,
        total_duration_min=float(total_duration_min),
        warnings=warnings,
    )
