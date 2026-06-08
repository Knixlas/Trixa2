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

Två modelleringsdetaljer:

1. **`pattern`** (crisscross/over-under). Ett segment kan bära ett `pattern` —
   en lista delsteg `{duration_min|duration_sec|duration_pct, zone|pct}` som
   växlar inom repet (1 min Z4 / 1 min Z3 …). Två former:
   - med `duration_min`/`duration_pct` på segmentet = *block*: varje set är ett
     block som fylls med så många pattern-cykler som ryms, vila mellan blocken
     (bike ME3 Crisscross).
   - utan blocklängd = *kontinuerligt*: `sets` = antal cykler i rad (run ME3).
   Delsteg kan ange exakt `pct: [lo, hi]` (för over-under där 103 % vs 99 % båda
   ligger i Z4) eller `zone: N`.

2. **Budget-normalisering**. Huvudset är fasta; bara `duration_pct`-segment
   (warmup/cooldown) skalas. De delar den *kvarvarande* budgeten
   (total − fasta segment) proportionellt mot sina pct-värden, så totalen blir
   exakt oavsett om pct-summan är 0.35 (bike) eller 1.0 (run). Matchar intentet
   i run-bankens header ("Bara warmup/cooldown skalas med budget").

Begränsningar (returneras som `warnings`, inte tyst tappade):
- swim distans→tid kräver CSS; utan CSS hoppas distansbaserade steg över.
- terrängstyrda pass (MF2 "hitta 4-6 backar") bär en representativ zon —
  strukturen kan inte föreskriva backarna; detaljen bär i `effort_descriptor`.
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


def _sub_seconds(sub: dict, total_seconds: int) -> int:
    """Längd för ett pattern-delsteg (duration_min | duration_sec | duration_pct)."""
    if sub.get("duration_min") is not None:
        return int(float(sub["duration_min"]) * 60)
    if sub.get("duration_sec") is not None:
        return int(float(sub["duration_sec"]))
    if sub.get("duration_pct") is not None:
        return int(float(sub["duration_pct"]) * total_seconds)
    return 0


def _sub_intensity(discipline: str, sub: dict) -> tuple[float, float]:
    """Intensitet för ett pattern-delsteg: exakt `pct: [lo,hi]` eller `zone: N`.

    `pct` behövs för over-under där t.ex. 103 % och 99 % FTP båda ligger i Z4 men
    ska skilja sig åt i strukturen.
    """
    pct = sub.get("pct")
    if isinstance(pct, (list, tuple)) and len(pct) == 2:
        return (float(pct[0]), float(pct[1]))
    return _intensity_pct(discipline, int(sub.get("zone", 2)))


def _build_pattern(
    seg: dict, seg_type: str, discipline: str,
    cadence: tuple[int, int] | None, total_seconds: int,
) -> tuple[list[dict], int]:
    """Expandera ett `pattern`-segment (crisscross/over-under) → (steps, sekunder).

    Block-form (segmentet har duration_min/duration_pct): varje set är ett block
    som fylls med pattern-cykler, vila mellan blocken. Kontinuerlig form (ingen
    blocklängd): `sets` cykler i rad, ev. vila inuti repetitionen.
    """
    pattern = seg.get("pattern") or []
    cycle_steps: list[dict] = []
    cycle_secs = 0
    for sub in pattern:
        sub_secs = _sub_seconds(sub, total_seconds)
        if sub_secs <= 0:
            continue
        cycle_steps.append(
            _step(seg_type, sub_secs, _sub_intensity(discipline, sub), "active", cadence)
        )
        cycle_secs += sub_secs
    if not cycle_steps or cycle_secs <= 0:
        return [], 0

    sets = _resolve_sets(seg.get("sets")) if "sets" in seg else 1
    rest_sec = int(seg.get("rest_sec") or 0)
    name = seg.get("description") or seg_type

    block_secs: int | None = None
    if seg.get("duration_min") is not None:
        block_secs = int(float(seg["duration_min"]) * 60)
    elif seg.get("duration_pct") is not None:
        block_secs = int(float(seg["duration_pct"]) * total_seconds)

    steps: list[dict] = []
    secs = 0

    if block_secs:
        # Block-form: fyll varje block med hela pattern-cykler, vila mellan block.
        inner_reps = max(1, round(block_secs / cycle_secs))
        for i in range(sets):
            if inner_reps == 1:
                steps.extend(dict(s) for s in cycle_steps)
            else:
                steps.append({
                    "type": "repetition", "name": name,
                    "reps": inner_reps, "steps": [dict(s) for s in cycle_steps],
                })
            secs += inner_reps * cycle_secs
            if rest_sec > 0 and i < sets - 1:
                steps.append(_step("Vila", rest_sec, _intensity_pct(discipline, 1), "rest"))
                secs += rest_sec
    else:
        # Kontinuerlig form: sets cykler i rad (run ME3). Vila inuti repet om angiven.
        inner = [dict(s) for s in cycle_steps]
        if rest_sec > 0:
            inner.append(_step("Vila", rest_sec, _intensity_pct(discipline, 1), "rest"))
        steps.append({"type": "repetition", "name": name, "reps": sets, "steps": inner})
        secs = sets * (cycle_secs + rest_sec)

    return steps, secs


def _build_fixed_segment(
    seg: dict, seg_type: str, klass: str, discipline: str, zone: int,
    cadence: tuple[int, int] | None, total_seconds: int,
    css_sec_per_100m: float | None, threshold_pace_sec_per_km: float | None,
    warnings: list[str], code: str,
) -> tuple[list[dict], int]:
    """Bygg ett fast (icke-pct-skalat) segment → (steps, sekunder).

    Hanterar `pattern` (crisscross/over-under) samt vanliga reps via
    duration_min / distance_m. Returnerar ([], 0) om inget byggbart (t.ex.
    distansbaserat utan pace) och loggar i så fall en varning.
    """
    if seg.get("pattern"):
        return _build_pattern(seg, seg_type, discipline, cadence, total_seconds)

    sets = _resolve_sets(seg.get("sets")) if "sets" in seg else 1
    per_rep_seconds: int | None = None
    if seg.get("duration_min") is not None:
        per_rep_seconds = int(float(seg["duration_min"]) * 60)
    elif seg.get("duration_sec") is not None:
        per_rep_seconds = int(float(seg["duration_sec"]))
    elif seg.get("distance_m") is not None:
        if discipline == "swim" and css_sec_per_100m:
            per_rep_seconds = int(seg["distance_m"] / 100.0 * css_sec_per_100m)
        elif discipline == "run" and threshold_pace_sec_per_km:
            lo_t, hi_t = _RUN_PACE_FRACTIONS[max(1, min(5, zone))]
            pace_factor = (lo_t + hi_t) / 2.0
            per_rep_seconds = int(
                seg["distance_m"] / 1000.0 * threshold_pace_sec_per_km * pace_factor
            )
        else:
            warnings.append(
                f"{code}: distansbaserat '{seg_type}' utan pace/CSS — "
                "hoppat över i strukturen."
            )
            return [], 0

    if not per_rep_seconds or per_rep_seconds <= 0:
        return [], 0

    intensity = _intensity_pct(discipline, zone)
    rest_sec = int(seg.get("rest_sec") or 0)

    if sets > 1:
        inner = [_step(seg_type, per_rep_seconds, intensity, klass, cadence)]
        if rest_sec > 0:
            inner.append(_step("Vila", rest_sec, _intensity_pct(discipline, 1), "rest"))
        return (
            [{"type": "repetition", "name": seg.get("description") or seg_type,
              "reps": sets, "steps": inner}],
            sets * (per_rep_seconds + rest_sec),
        )
    return [_step(seg_type, per_rep_seconds, intensity, klass, cadence)], per_rep_seconds


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
    code = workout.get("code", "?")
    warnings: list[str] = []
    total_seconds = int(total_duration_min * 60)
    metric = INTENSITY_METRIC.get(discipline, "percentOfFtp")

    # Två pass: först bygg fasta segment och samla flex-segmentens (warmup/
    # cooldown) pct-andelar; sen fördela kvarvarande budget proportionellt.
    entries: list[tuple[str, Any]] = []
    fixed_seconds = 0
    pct_sum = 0.0

    for seg in workout.get("main_set", []):
        seg_type = seg.get("segment", "main")
        zone = _seg_zone(seg)

        cadence = None
        rpm = seg.get("cadence_rpm")
        if isinstance(rpm, list) and len(rpm) == 2:
            cadence = (int(rpm[0]), int(rpm[1]))

        if seg_type == "warmup":
            klass = "warmUp"
        elif seg_type == "cooldown":
            klass = "coolDown"
        else:
            klass = "active"

        # Legacy-skydd: effort_descriptor utan zon/pattern → approximera till Z4.
        if (seg.get("effort_descriptor") and seg.get("zone") is None
                and not seg.get("pattern") and not seg.get("zones_per_set")):
            warnings.append(
                f"{code}: '{seg_type}' har effort_descriptor utan zon — "
                "approximerat till Z4-block, detaljen bär i texten."
            )
            zone = 4

        # Flex-segment = duration_pct-skalat (warmup/cooldown). De delar budgeten.
        # OBS: resolve_template skriver ett *naivt* duration_min (pct×total) jämte
        # duration_pct — vi ignorerar det och normaliserar om mot den faktiska
        # kvarvarande budgeten här (mapping har pace/CSS, det har inte templates.py).
        # Pattern eller distans gör segmentet fast.
        is_flex = (
            seg.get("duration_pct") is not None
            and seg.get("distance_m") is None
            and not seg.get("pattern")
        )
        if is_flex:
            pct = float(seg["duration_pct"])
            pct_sum += pct
            entries.append(("flex", {
                "seg_type": seg_type, "zone": zone, "klass": klass,
                "cadence": cadence, "pct": pct,
            }))
            continue

        built, secs = _build_fixed_segment(
            seg, seg_type, klass, discipline, zone, cadence, total_seconds,
            css_sec_per_100m, threshold_pace_sec_per_km, warnings, code,
        )
        if built:
            entries.append(("fixed", built))
            fixed_seconds += secs

    flexible = max(0, total_seconds - fixed_seconds)
    if pct_sum > 0 and flexible == 0:
        warnings.append(
            f"{code}: fasta segment ({fixed_seconds // 60} min) fyller hela passet "
            f"({int(total_duration_min)} min) — ingen uppvärmning/nedvarvning fick plats."
        )

    steps: list[dict] = []
    for kind, payload in entries:
        if kind == "fixed":
            steps.extend(payload)
        else:
            secs = int(round(payload["pct"] / pct_sum * flexible)) if pct_sum else 0
            if secs > 0:
                steps.append(_step(
                    payload["seg_type"], secs,
                    _intensity_pct(discipline, payload["zone"]),
                    payload["klass"], payload["cadence"],
                ))

    if not steps:
        warnings.append(f"{code}: inga byggbara steg.")

    structure = {"primaryIntensityMetric": metric, "steps": steps}
    return TPStructureResult(
        sport=SPORT_NAME.get(discipline, "Other"),
        structure=structure,
        total_duration_min=float(total_duration_min),
        warnings=warnings,
    )
