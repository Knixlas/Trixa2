"""TP wire-struktur + IF/TSS + create-payload.

Den förenklade strukturen (mapping.py) måste konverteras till TP:s
*wire-format* innan POST: nästlade block med kumulativa begin/end, targets
och en polyline. IF/TSS räknas NP-style (4:e-potens-viktat snitt) — samma
matematik som JamsusMaximus/trainingpeaks-mcp (MIT), porterad till rena
dictar utan pydantic/async.

`build_create_payload()` sätter ihop hela kroppen för
`POST /fitness/v6/athletes/{id}/workouts`:
- `workoutDay` (midnatt), ev. `startTimePlanned`
- `workoutTypeFamilyId`/`workoutTypeValueId` ur SPORT_TYPE_MAP
- `totalTimePlanned` i **timmar** (TP-konvention)
- `tssPlanned`, `ifPlanned`
- `structure` som JSON-sträng (wire)
"""

from __future__ import annotations

import json
from datetime import date as date_type
from datetime import datetime as datetime_type
from typing import Any

from .mapping import TPStructureResult

# Sportnamn → (workoutTypeFamilyId, workoutTypeValueId). Bekräftat mot
# GET /fitness/v6/workouttypes (se MCP workouts.py SPORT_TYPE_MAP).
SPORT_TYPE_MAP: dict[str, tuple[int, int]] = {
    "Swim": (1, 1), "Bike": (2, 2), "Run": (3, 3), "Brick": (4, 4),
    "Crosstrain": (5, 5), "Race": (6, 6), "DayOff": (7, 7), "MtnBike": (8, 8),
    "Strength": (9, 9), "Custom": (10, 10), "XCSki": (11, 11), "Rowing": (12, 12),
    "Walk": (13, 13), "Other": (100, 100),
}

# Sporter vars strukturerade pass når Garmin-klockan via TP→Garmin AutoSync.
AUTOSYNC_ELIGIBLE = {"Swim", "Bike", "Run", "Crosstrain", "MtnBike", "Rowing",
                     "Walk", "Custom", "Other"}


def _iter_leaf_steps(structure: dict) -> Any:
    """Generator över (duration_seconds, intensity_min, intensity_max) för varje
    faktiskt utfört steg (repetition-block expanderas)."""
    for block in structure.get("steps", []):
        if block.get("type") == "repetition":
            for _ in range(int(block["reps"])):
                for s in block["steps"]:
                    yield s
        else:
            yield block


def compute_if_tss(structure: dict) -> tuple[float, float, int]:
    """(IF, TSS, total_seconds) NP-style ur den förenklade strukturen."""
    weighted = 0.0
    total = 0
    for s in _iter_leaf_steps(structure):
        mid = (s["intensity_min"] + s["intensity_max"]) / 2.0
        weighted += s["duration_seconds"] * (mid ** 4)
        total += s["duration_seconds"]
    if total == 0:
        return 0.0, 0.0, 0
    intensity_factor = (weighted / total) ** 0.25 / 100.0
    tss = (total * intensity_factor ** 2 * 100.0) / 3600.0
    return round(intensity_factor, 3), round(tss, 1), total


def _step_wire(s: dict) -> dict:
    targets: list[dict] = [{"minValue": s["intensity_min"], "maxValue": s["intensity_max"]}]
    if s.get("cadence_min") is not None and s.get("cadence_max") is not None:
        targets.append({
            "minValue": s["cadence_min"], "maxValue": s["cadence_max"],
            "unit": "roundOrStridePerMinute",
        })
    return {
        "name": s["name"],
        "type": "step",
        "length": {"value": s["duration_seconds"], "unit": "second"},
        "targets": targets,
        "intensityClass": s.get("intensityClass", "active"),
        "openDuration": False,
    }


def _block_duration(block: dict) -> int:
    if block.get("type") == "repetition":
        inner = sum(s["duration_seconds"] for s in block["steps"])
        return inner * int(block["reps"])
    return int(block["duration_seconds"])


def build_wire(structure: dict) -> dict:
    """Förenklad struktur → TP wire-format (block med begin/end + polyline)."""
    steps = structure.get("steps", [])
    metric = structure.get("primaryIntensityMetric", "percentOfFtp")
    total = sum(_block_duration(b) for b in steps)

    wire_blocks: list[dict] = []
    cum = 0
    for b in steps:
        dur = _block_duration(b)
        begin, end = cum, cum + dur
        if b.get("type") == "repetition":
            wire_blocks.append({
                "type": "repetition",
                "length": {"value": int(b["reps"]), "unit": "repetition"},
                "steps": [_step_wire(s) for s in b["steps"]],
                "begin": begin, "end": end,
            })
        else:
            wire_blocks.append({
                "type": "step",
                "length": {"value": 1, "unit": "repetition"},
                "steps": [_step_wire(b)],
                "begin": begin, "end": end,
            })
        cum = end

    polyline: list[list[float]] = []
    pc = 0

    def _bar(t0: float, t1: float, inten: float) -> None:
        polyline.extend([
            [round(t0, 4), 0], [round(t0, 4), round(inten, 4)],
            [round(t1, 4), round(inten, 4)], [round(t1, 4), 0],
        ])

    for s in _iter_leaf_steps(structure):
        t0 = pc / total if total else 0
        pc += s["duration_seconds"]
        t1 = pc / total if total else 0
        _bar(t0, t1, s["intensity_max"] / 100.0)

    return {
        "structure": wire_blocks,
        "polyline": polyline,
        "primaryLengthMetric": "duration",
        "primaryIntensityMetric": metric,
        "primaryIntensityTargetOrRange": "range",
    }


def build_create_payload(
    result: TPStructureResult,
    workout_day: date_type | datetime_type,
    title: str,
    description: str | None = None,
    tss_override: float | None = None,
) -> dict:
    """Bygg kroppen för POST .../workouts. `athleteId` sätts av klienten."""
    if result.sport not in SPORT_TYPE_MAP:
        raise ValueError(f"Okänd sport: {result.sport}")
    family_id, type_id = SPORT_TYPE_MAP[result.sport]

    wire = build_wire(result.structure)
    intensity_factor, tss, total_seconds = compute_if_tss(result.structure)

    day = workout_day.date() if isinstance(workout_day, datetime_type) else workout_day
    payload: dict[str, Any] = {
        "workoutDay": f"{day.isoformat()}T00:00:00",
        "workoutTypeFamilyId": family_id,
        "workoutTypeValueId": type_id,
        "title": title,
        "totalTimePlanned": round(total_seconds / 3600.0, 4),  # timmar
        "tssPlanned": tss_override if tss_override is not None else tss,
        "ifPlanned": intensity_factor,
        "structure": json.dumps(wire),
    }
    if isinstance(workout_day, datetime_type):
        payload["startTimePlanned"] = workout_day.isoformat(timespec="seconds")
    if description:
        payload["description"] = description
    return payload
