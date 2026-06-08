"""WeekPlan/passbank-pass → TrainingPeaks planerade pass.

Ersätter den aldrig-byggda `.fit`-exporten. Ett pass blir ett strukturerat
TP-pass; TP→Garmin AutoSync levererar nästa 15 dagar till klockan.

Brick & Strength når **inte** klockan via AutoSync (se docs/06 §7). De skapas
ändå i TP (synliga i appen) men flaggas i `warnings` så coachen vet.

Planner-loopen (WeekPlan → iterera → create) sitter i task 7-wiringen; den här
modulen exponerar per-pass-funktionen som loopen anropar, plus en batch-helper.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime as datetime_type
from datetime import timedelta, timezone
from typing import Any

from .client import TPClient, TPNotFoundError
from .mapping import build_tp_structure
from .structure import AUTOSYNC_ELIGIBLE, SPORT_TYPE_MAP, build_create_payload


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


_PS_SPORT_TO_DISCIPLINE = {
    "Sim": "swim", "Cykel": "bike", "Löpning": "run", "Lopning": "run",
    "Styrka": "strength", "Vila": "rest", "Brick": "brick",
}


def push_week_from_planned_sessions(
    client: TPClient | None,
    pg: Any,
    user_id: str,
    week_start: date_type,
    css_sec_per_100m: float | None = None,
    threshold_pace_sec_per_km: float | None = None,
    dry_run: bool = False,
) -> list[WriteResult]:
    """Läs veckans rader ur MASTER planned_sessions och skapa strukturerade
    TP-pass av dem (→ TP→Garmin AutoSync → klockan).

    Det här är vägen för "Nils/Trixa2 har planerat/ändrat i planned_sessions →
    skicka till klockan". Vila/strukturlösa rader hoppas över.
    """
    week_end = (week_start + timedelta(days=6)).isoformat()
    rows = (
        pg.table("planned_sessions")
        .select("date, sport, title, workout_code, duration_min, steps")
        .eq("user_id", user_id)
        .gte("date", week_start.isoformat())
        .lte("date", week_end)
        .order("date")
        .execute()
    ).data or []

    results: list[WriteResult] = []
    for r in rows:
        discipline = _PS_SPORT_TO_DISCIPLINE.get(r.get("sport"), (r.get("sport") or "").lower())
        steps = r.get("steps") or []
        if discipline == "rest" or not steps:
            continue
        workout = {
            "discipline": discipline,
            "main_set": steps,
            "code": r.get("workout_code"),
            "name": r.get("title"),
            "intent": "",
        }
        day = date_type.fromisoformat(str(r["date"])[:10])
        results.append(create_planned_workout(
            client, workout, day, r.get("duration_min") or 60,
            css_sec_per_100m=css_sec_per_100m,
            threshold_pace_sec_per_km=threshold_pace_sec_per_km,
            title=r.get("title"),
            dry_run=dry_run,
        ))
    return results


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


# ---------- Idempotent vecko-sync (replace-by-id, skip-if-unchanged) ----------


@dataclass
class SyncResult:
    code: str
    title: str
    day: str
    sport: str
    action: str  # created | replaced | unchanged | would_create | would_replace
    workout_id: int | None = None
    reaches_watch: bool = True
    warnings: list[str] = field(default_factory=list)


def _payload_hash(payload: dict) -> str:
    """Stabil hash av det som faktiskt levereras till TP (titel/dag/tss/struktur)."""
    key = json.dumps(
        {"t": payload.get("title"), "d": payload.get("workoutDay"),
         "tss": payload.get("tssPlanned"), "s": payload.get("structure")},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _find_existing_tp_id(existing_tp: list[dict], day_iso: str, type_id: int) -> int | None:
    """Matcha ett planerat (ej genomfört) TP-pass på (dag, sporttyp).

    Fallback för rader som pushats utan lagrat tp_workout_id (t.ex. innan
    spårningskolumnerna fanns). Rör aldrig genomförda pass (totalTime satt).
    """
    for w in existing_tp:
        wd = (w.get("workoutDay") or w.get("startTime") or "")[:10]
        if wd != day_iso or w.get("workoutTypeValueId") != type_id:
            continue
        if w.get("totalTime"):
            continue
        return w.get("workoutId")
    return None


def sync_planned_week_to_tp(
    client: TPClient | None,
    pg: Any,
    user_id: str,
    week_start: date_type,
    css_sec_per_100m: float | None = None,
    threshold_pace_sec_per_km: float | None = None,
    dry_run: bool = False,
) -> list[SyncResult]:
    """Idempotent push av veckans planned_sessions → TrainingPeaks.

    Per rad: bygg payload + hash, hitta ev. befintligt TP-pass (lagrat
    ``tp_workout_id``, annars matchning på dag+sport). Oförändrat (samma hash) →
    hoppa (ingen churn). Ändrat/nytt → radera ev. gammalt + skapa, och spara
    ``tp_workout_id``/``tp_synced_hash``/``tp_synced_at`` på raden. Skapar
    **aldrig dubbletter** — säker att köra dagligen.
    """
    week_end = (week_start + timedelta(days=6)).isoformat()
    rows = (
        pg.table("planned_sessions")
        .select("id,date,sport,title,workout_code,duration_min,steps,tp_workout_id,tp_synced_hash")
        .eq("user_id", user_id)
        .gte("date", week_start.isoformat())
        .lte("date", week_end)
        .order("date")
        .execute()
    ).data or []

    existing_tp: list[dict] = []
    if client is not None:
        try:
            existing_tp = client.get_workouts(week_start, week_start + timedelta(days=6))
        except Exception:  # noqa: BLE001 — fallback-matchning är best-effort
            existing_tp = []

    now_iso = datetime_type.now(timezone.utc).isoformat()
    results: list[SyncResult] = []
    for r in rows:
        discipline = _PS_SPORT_TO_DISCIPLINE.get(r.get("sport"), (r.get("sport") or "").lower())
        steps = r.get("steps") or []
        if discipline == "rest" or not steps:
            continue
        day = date_type.fromisoformat(str(r["date"])[:10])
        total = r.get("duration_min") or 60
        workout = {"discipline": discipline, "main_set": steps,
                   "code": r.get("workout_code"), "name": r.get("title"), "intent": ""}
        res = build_tp_structure(workout, total, css_sec_per_100m, threshold_pace_sec_per_km)
        title = r.get("title") or r.get("workout_code") or "Pass"
        reaches_watch = res.sport in AUTOSYNC_ELIGIBLE
        payload = build_create_payload(res, day, title)
        new_hash = _payload_hash(payload)

        existing_id = r.get("tp_workout_id")
        if not existing_id and res.sport in SPORT_TYPE_MAP:
            existing_id = _find_existing_tp_id(
                existing_tp, day.isoformat(), SPORT_TYPE_MAP[res.sport][1])

        warnings = list(res.warnings)
        if not reaches_watch:
            warnings.append(
                f"{workout['code']}: {res.sport} når ej klockan via TP→Garmin AutoSync."
            )

        # Oförändrat → hoppa (ingen churn, idempotent no-op).
        if existing_id and r.get("tp_synced_hash") == new_hash:
            results.append(SyncResult(
                r.get("workout_code") or "?", title, day.isoformat(), res.sport,
                "unchanged", existing_id, reaches_watch, warnings))
            continue

        if dry_run or client is None:
            results.append(SyncResult(
                r.get("workout_code") or "?", title, day.isoformat(), res.sport,
                "would_replace" if existing_id else "would_create",
                existing_id, reaches_watch, warnings))
            continue

        if existing_id:
            try:
                client.delete_workout(existing_id)
            except TPNotFoundError:
                pass
        created = client.create_workout(payload)
        new_id = created.get("workoutId")
        (pg.table("planned_sessions")
            .update({"tp_workout_id": new_id, "tp_synced_hash": new_hash, "tp_synced_at": now_iso})
            .eq("id", r["id"]).execute())
        results.append(SyncResult(
            r.get("workout_code") or "?", title, day.isoformat(), res.sport,
            "replaced" if existing_id else "created", new_id, reaches_watch, warnings))
    return results
