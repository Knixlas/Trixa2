"""WeekPlan/passbank-pass → TrainingPeaks planerade pass.

Ersätter den aldrig-byggda `.fit`-exporten. Ett pass blir ett strukturerat
TP-pass; TP→Garmin AutoSync levererar nästa 15 dagar till klockan.

Brick & Strength når **inte** klockan via AutoSync (se docs/06 §7). De skapas
ändå i TP (synliga i appen) men flaggas i `warnings` så coachen vet.

Planner-loopen (WeekPlan → iterera → create) sitter i task 7-wiringen; den här
modulen exponerar per-pass-funktionen som loopen anropar, plus en batch-helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime as datetime_type
from typing import Any

from .client import TPClient
from .mapping import build_tp_structure
from .structure import AUTOSYNC_ELIGIBLE, build_create_payload


@dataclass
class WriteResult:
    code: str
    title: str
    day: str
    sport: str
    reaches_watch: bool
    workout_id: int | None = None
    dry_run: bool = False
    warnings: list[str] = field(default_factory=list)


def create_planned_workout(
    client: TPClient | None,
    workout_pass: dict,
    day: date_type | datetime_type,
    total_duration_min: float,
    css_sec_per_100m: float | None = None,
    threshold_pace_sec_per_km: float | None = None,
    title: str | None = None,
    dry_run: bool = False,
) -> WriteResult:
    """Skapa ett planerat TP-pass ur ett passbank-pass.

    Args:
        client: TPClient (krävs ej i dry_run).
        workout_pass: parsad YAML-post.
        day: kalenderdag (date) eller exakt starttid (datetime).
        total_duration_min: konkret total (planner-budget).
        css_sec_per_100m: för swim distans→tid.
        threshold_pace_sec_per_km: för run distans→tid.
        title: override; default passets `name`.
        dry_run: bygg payload men POST:a inte.
    """
    res = build_tp_structure(
        workout_pass, total_duration_min, css_sec_per_100m, threshold_pace_sec_per_km
    )
    title = title or workout_pass.get("name") or workout_pass.get("code", "Pass")
    description = workout_pass.get("intent")

    payload = build_create_payload(res, day, title, description=description)

    warnings = list(res.warnings)
    reaches_watch = res.sport in AUTOSYNC_ELIGIBLE
    if not reaches_watch:
        warnings.append(
            f"{workout_pass.get('code','?')}: {res.sport} synkar inte till klockan "
            "via TP→Garmin AutoSync (skapas i TP men levereras ej till device)."
        )

    day_str = (day.date() if isinstance(day, datetime_type) else day).isoformat()

    if dry_run or client is None:
        return WriteResult(
            code=workout_pass.get("code", "?"), title=title, day=day_str,
            sport=res.sport, reaches_watch=reaches_watch, dry_run=True,
            warnings=warnings,
        )

    created = client.create_workout(payload)
    return WriteResult(
        code=workout_pass.get("code", "?"), title=title, day=day_str,
        sport=res.sport, reaches_watch=reaches_watch,
        workout_id=created.get("workoutId"), warnings=warnings,
    )


def create_week(
    client: TPClient | None,
    items: list[dict],
    dry_run: bool = False,
) -> list[WriteResult]:
    """Batch: skapa flera pass. Varje item:
        {"workout": <pass-dict>, "day": date|datetime,
         "total_duration_min": float, "css_sec_per_100m": float|None,
         "threshold_pace_sec_per_km": float|None, "title": str|None}
    """
    results: list[WriteResult] = []
    for it in items:
        results.append(create_planned_workout(
            client,
            it["workout"],
            it["day"],
            it["total_duration_min"],
            css_sec_per_100m=it.get("css_sec_per_100m"),
            threshold_pace_sec_per_km=it.get("threshold_pace_sec_per_km"),
            title=it.get("title"),
            dry_run=dry_run,
        ))
    return results
