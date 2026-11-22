"""AI-coachers förslag till passbanken, och adeptens passkänsla.

En coach (via MCP) kan föreslå ett nytt pass, helst ett som bygger på pass
adepten gjort och gillat. Förslaget går INTE in i passbanken: det sparas i
``workout_proposals`` (migration 014) med status ``pending``, granskas i
``/ui/admin/forslag`` och importeras till YAML av
``coach.trixa.import_proposals`` — så att det går genom validator,
språkkontroll och tester innan någon adept får det.

Vid inlämning prövas förslaget som ett riktigt pass:
  - formen (obligatoriska fält, giltig gren, kategori, faser, zoner),
  - att det går att rendera för adepten,
  - språket (coach/engine/sprak.py — samma regler som passbanken),
  - att det inte bär adeptens namn eller hälsouppgifter,
  - att det bygger på pass adepten faktiskt gjort (källpassen måste vara
    adeptens egna och minst ett loggat).
Underlaget (gjorda pass, svaren på "Vill du göra passet igen?", snitt-
ansträngning) räknas av servern ur loggen, inte ur coachens påstående.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from coach.trixa import clock, sports

_PROPOSABLE_SPORTS = ("swim", "bike", "run", "strength", "brick")
_SEGMENT_KEYS = {
    "segment", "duration_pct", "duration_min", "distance_m", "sets", "rest_sec",
    "zone", "zones_per_set", "effort_descriptor", "description", "cadence_rpm",
    "drills", "exercise", "prescription", "alt", "note", "load_pct", "sport",
    "equipment",
}
_DEFAULT_PHASES = ["prep", "base_1", "base_2", "base_3", "build_1", "build_2"]


# ---------- passkänsla ----------


def feedback_by_session(client, user_id: str, session_ids: list[str]) -> dict[str, str]:
    """planned_session_id → "yes"/"maybe"/"no". Tom dict om tabellen saknas."""
    if not session_ids:
        return {}
    try:
        rows = (
            client.table("session_feedback").select("planned_session_id, again")
            .eq("user_id", user_id).in_("planned_session_id", session_ids).execute()
        ).data or []
    except Exception:  # noqa: BLE001 — migration 014 ej körd
        return {}
    return {str(r["planned_session_id"]): r.get("again") for r in rows}


def session_feedback_summary(client, user_id: str, days: int = 90) -> dict:
    """Adeptens gjorda pass med svar och ansträngning, och per pass-typ.

    Det coachen behöver för att se vilka pass adepten gillat innan hen
    föreslår något nytt till passbanken.
    """
    since = (clock.today() - timedelta(days=days)).isoformat()
    try:
        fb = (
            client.table("session_feedback")
            .select("planned_session_id, workout_code, session_date, again")
            .eq("user_id", user_id).gte("session_date", since).execute()
        ).data or []
    except Exception:  # noqa: BLE001
        fb = []
    ids = [str(r["planned_session_id"]) for r in fb if r.get("planned_session_id")]
    planned = {}
    rpe: dict[str, list[float]] = {}
    if ids:
        for r in (
            client.table("planned_sessions").select("id, date, sport, title, workout_code")
            .eq("user_id", user_id).in_("id", ids).execute()
        ).data or []:
            planned[str(r["id"])] = r
        for r in (
            client.table("training_log").select("planned_session_id, rpe")
            .eq("user_id", user_id).in_("planned_session_id", ids).execute()
        ).data or []:
            if r.get("rpe") is not None:
                rpe.setdefault(str(r["planned_session_id"]), []).append(float(r["rpe"]))
    sessions = []
    by_kind: dict[str, dict[str, Any]] = {}
    for r in fb:
        sid = str(r.get("planned_session_id") or "")
        p = planned.get(sid, {})
        kind = r.get("workout_code") or p.get("title") or "okänt"
        sessions.append({
            "planned_session_id": sid or None, "date": r.get("session_date"),
            "sport": sports.canon(p.get("sport")), "title": p.get("title"),
            "workout_code": r.get("workout_code") or "", "again": r.get("again"),
            "rpe": (sum(rpe[sid]) / len(rpe[sid])) if rpe.get(sid) else None,
        })
        agg = by_kind.setdefault(kind, {"kind": kind, "yes": 0, "maybe": 0, "no": 0})
        if r.get("again") in ("yes", "maybe", "no"):
            agg[r["again"]] += 1
    liked = sorted(by_kind.values(), key=lambda a: (-a["yes"], a["no"]))
    return {"days": days, "sessions": sessions, "by_kind": liked}


# ---------- förslag ----------


def _texts(w: dict) -> list[tuple[str, str]]:
    out = [("name", w.get("name") or ""), ("intent", w.get("intent") or "")]
    for i, seg in enumerate(w.get("main_set") or []):
        for f in ("description", "effort_descriptor", "note"):
            if seg.get(f):
                out.append((f"main_set[{i}].{f}", str(seg[f])))
    for i, c in enumerate(w.get("abort_conditions") or []):
        out.append((f"abort_conditions[{i}]", str(c)))
    return out


def _private_words(client, athlete: dict, user_id: str) -> list[str]:
    """Ord som inte får stå i ett pass som ska delas: namn och hälsouppgifter."""
    words: list[str] = []
    try:
        prof = (
            client.table("profiles").select("name").eq("id", user_id).limit(1).execute()
        ).data or []
        if prof and prof[0].get("name"):
            words += [p for p in str(prof[0]["name"]).split() if len(p) >= 3]
    except Exception:  # noqa: BLE001
        pass
    for key in ("active_concerns", "health_conditions"):
        for item in athlete.get(key) or []:
            if isinstance(item, dict):
                for f in ("name", "medication"):
                    if item.get(f) and len(str(item[f])) >= 4:
                        words.append(str(item[f]))
    for med in athlete.get("medications") or []:
        if isinstance(med, str) and len(med) >= 4:
            words.append(med)
    return words


def _build_workout(body: dict, sport_key: str) -> dict:
    duration = body.get("duration_min") or {}
    if isinstance(duration, (int, float)):
        duration = {"default": int(duration)}
    default = int(duration.get("default") or 0)
    rng = duration.get("range") or ([max(10, round(default * 0.7)), round(default * 1.3)] if default else None)
    if not default or not rng or len(rng) != 2:
        raise HTTPException(400, "duration_min behöver default och range [min, max].")
    main_set = [
        {k: v for k, v in seg.items() if k in _SEGMENT_KEYS}
        for seg in (body.get("main_set") or []) if isinstance(seg, dict)
    ]
    zones = sorted({
        f"Z{seg['zone']}" for seg in main_set if isinstance(seg.get("zone"), int)
    } | {f"Z{z}" for seg in main_set for z in seg.get("zones_per_set") or [] if isinstance(z, int)})
    category = str(body.get("category") or "").strip()
    return {
        "code": f"FORSLAG_{sport_key}_{uuid.uuid4().hex[:6]}",
        "discipline": sport_key,
        "category": category,
        "type_code": (str(body.get("type_code") or "").strip()
                      or (f"{category}2" if category == "AE" else category)),
        "name": str(body.get("name") or "").strip(),
        "phase_appropriate": list(body.get("phases") or _DEFAULT_PHASES),
        "parameterized": True,
        "parameters": {"duration_min": {"default": default, "range": [int(rng[0]), int(rng[1])]}},
        "intent": str(body.get("intent") or "").strip(),
        "main_set": main_set,
        "total_duration_min": {"estimated": "{duration_min}", "flexible_range": [int(rng[0]), int(rng[1])]},
        "zone_refs": zones,
        "equipment": list(body.get("equipment") or []),
        "abort_conditions": [str(c) for c in body.get("abort_conditions") or []],
    }


def _check(workout: dict) -> tuple[list[str], str]:
    """Formen, renderingen och språket. Returnerar (fel, förhandsvisning)."""
    from coach.engine import validator as v
    from coach.engine.loader import AthleteProfile, load_drills, load_strength_exercises
    from coach.engine.renderer import render_workout
    from coach.engine.sprak import jargon
    from coach.engine.templates import resolve_template

    errors = (
        v._check_required_fields([workout], v._REQUIRED_WORKOUT_FIELDS, "Pass")
        + v._check_enums([workout]) + v._check_zones([workout])
    )
    if not workout["name"]:
        errors.append("name saknas.")
    if not workout["intent"]:
        errors.append("intent saknas — beskriv passet för adepten.")
    if not workout["main_set"]:
        errors.append("main_set saknas — passets delar (uppvärmning, huvuddel, nedvarvning).")
    drills = {d["code"]: d for d in load_drills()}
    exercises = {e["code"]: e for e in load_strength_exercises()}
    errors += v._check_drill_refs([workout], set(drills))
    for seg in workout["main_set"]:
        if seg.get("segment") == "strength_block" and seg.get("exercise") not in exercises:
            errors.append(f"Övningen {seg.get('exercise')!r} finns inte i övningskatalogen.")
    for field, text in _texts(workout):
        found = jargon(text)
        if found:
            errors.append(f"{field}: fackord {sorted(set(found))} — se docs/PASSBANK_SPRAK.md.")
    preview = ""
    if not errors:
        try:
            preview = render_workout(resolve_template(workout), AthleteProfile(), drills, exercises)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Passet gick inte att rendera: {exc}")
    return errors, preview


def _evidence(client, user_id: str, ids: list[str]) -> dict:
    """Underlaget räknat ur adeptens egna data — inte ur coachens påstående."""
    if not ids:
        raise HTTPException(
            400, "source_session_ids krävs: förslaget ska bygga på pass adepten gjort. "
                 "Hitta dem med get_session_feedback eller get_week."
        )
    own = (
        client.table("planned_sessions").select("id, date, title, workout_code, sport")
        .eq("user_id", user_id).in_("id", ids).execute()
    ).data or []
    if len(own) != len(set(ids)):
        raise HTTPException(400, "Alla source_session_ids måste vara adeptens egna planerade pass.")
    logs = (
        client.table("training_log").select("planned_session_id, rpe")
        .eq("user_id", user_id).in_("planned_session_id", ids).execute()
    ).data or []
    done = {str(r["planned_session_id"]) for r in logs}
    if not done:
        raise HTTPException(400, "Inget av källpassen är loggat som gjort. Föreslå pass adepten faktiskt kört.")
    fb = feedback_by_session(client, user_id, ids)
    rpes = [float(r["rpe"]) for r in logs if r.get("rpe") is not None]
    return {
        "sessions": len(own),
        "done": len(done),
        "again": {k: sum(1 for a in fb.values() if a == k) for k in ("yes", "maybe", "no")},
        "avg_rpe": round(sum(rpes) / len(rpes), 1) if rpes else None,
        "sources": [
            {"date": r.get("date"), "title": r.get("title"), "workout_code": r.get("workout_code") or ""}
            for r in sorted(own, key=lambda r: str(r.get("date")))
        ],
    }


def propose_workout(client, athlete: dict, user_id: str, token_id: str | None,
                    proposer: str, body: dict) -> dict:
    """Pröva och spara ett passförslag (status pending)."""
    from coach.engine.loader import load_workouts

    sport_key = sports.canon(body.get("sport"))
    if sport_key not in _PROPOSABLE_SPORTS:
        raise HTTPException(400, f"sport måste vara en av {', '.join(_PROPOSABLE_SPORTS)}.")
    workout = _build_workout(body, sport_key)
    errors, preview = _check(workout)
    private = [
        w for w in _private_words(client, athlete, user_id)
        if any(w.casefold() in text.casefold() for _f, text in _texts(workout))
    ]
    if private:
        errors.append(
            "Förslaget innehåller adeptens namn eller hälsouppgifter. Passbanken "
            "delas med alla — skriv passet utan dem."
        )
    if errors:
        raise HTTPException(400, {"message": "Förslaget uppfyller inte passbankens krav.", "errors": errors})

    ids = [str(i) for i in body.get("source_session_ids") or []]
    evidence = _evidence(client, user_id, ids)
    warnings: list[str] = []
    if not evidence["again"]["yes"]:
        warnings.append(
            "Inget av källpassen har fått \"ja\" på frågan om adepten vill göra "
            "det igen. Förslaget granskas ändå, men det väger lättare."
        )
    if evidence["again"]["no"]:
        warnings.append(f"{evidence['again']['no']} av källpassen fick \"nej\".")
    existing = {w.get("name", "").casefold() for w in load_workouts()}
    if workout["name"].casefold() in existing:
        warnings.append("Det finns redan ett pass med samma namn i passbanken.")

    row = {
        "athlete_user_id": user_id,
        "proposed_via_token_id": token_id,
        "proposer_name": proposer,
        "sport": sport_key,
        "category": workout["category"],
        "name": workout["name"],
        "workout": workout,
        "rationale": str(body.get("rationale") or "").strip(),
        "source_session_ids": ids,
        "evidence": evidence,
        "status": "pending",
    }
    res = client.table("workout_proposals").insert(row).execute()
    pid = (res.data or [{}])[0].get("id")
    return {
        "status": "pending",
        "id": pid,
        "message": "Förslaget är sparat och väntar på granskning. Det når ingen adept förrän det godkänts och lagts in i passbanken.",
        "evidence": evidence,
        "warnings": warnings,
        "preview": preview,
    }


def list_proposals(client, user_id: str) -> dict:
    rows = (
        client.table("workout_proposals")
        .select("id, name, sport, category, status, review_note, reviewed_at, imported_code, created_at")
        .eq("athlete_user_id", user_id).order("created_at", desc=True).limit(50).execute()
    ).data or []
    return {"proposals": rows}


def review(client, proposal_id: str, reviewer_id: str, decision: str, note: str) -> dict:
    """Godkänn eller avböj. Bara pending-förslag kan granskas."""
    from datetime import datetime, timezone

    if decision not in ("accepted", "rejected"):
        raise HTTPException(400, "decision måste vara accepted eller rejected.")
    res = (
        client.table("workout_proposals").update({
            "status": decision, "review_note": note.strip() or None,
            "reviewed_by": reviewer_id, "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", proposal_id).eq("status", "pending").execute()
    )
    if not res.data:
        raise HTTPException(409, "Förslaget finns inte eller är redan granskat.")
    return res.data[0]

