"""Engångs-backfill: strukturera styrkepass som bär övningarna som prosa.

Två källor:
  - trixa2-rader från före TX-4: har `steps`, saknar `exercises`. Byggs med
    samma funktion planeraren använder i dag, inkl. mallens repspann via
    workout_code.
  - nils-rader: två prosaformat (numrerad " · "-lista, och markdown-bullets).
    Parsas här — INTE i produkten. Utfallet granskas för hand innan --write.

Tid, sträcka och "till utmattning" är inte reps. De lämnar reps tomt och
följer med i noten, så att progressionen inte räknar på sekunder.

Kör:  python backfill_exercises.py            (torrkörning)
      python backfill_exercises.py --write    (skriver exercises till planned_sessions)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from coach.engine.loader import load_strength_exercises, load_workouts  # noqa: E402
from coach.trixa.db import get_postgrest  # noqa: E402
from coach.trixa.exercise_plan import exercises_from_steps, normalize_exercises  # noqa: E402

USER = "acb82415-da4e-40c3-8696-a9d6d704d47f"
CATALOGUE = {e["code"]: e for e in load_strength_exercises()}
WORKOUTS = {w["code"]: w for w in load_workouts()}

NAME_TO_CODE = {
    "knäböj med kroppsvikt": "bodyweight_squat",
    "armhävning": "pushup",
    "upprätt rodd i ring/stång": "inverted_row",
    "gående utfall": "walking_lunge",
    "enbensstående balans": "single_leg_balance",
    "planka": "plank",
    "sidoplanka": "side_plank",
    "höftlyft": "hip_thrust",
    "step-up": "step_up",
}

RE_SETS_REPS = re.compile(r"(\d+)\s*(?:set)?\s*[×x]\s*(\d+)(?:\s*reps?)?", re.I)
RE_SETS_ONLY = re.compile(r"^(\d+)\s*(?:set)?\s*[×x]?", re.I)
RE_RIR = re.compile(r"RIR\s*(\d)", re.I)
RE_REST = re.compile(r"(\d+)\s*s(?:ek)?\s*vila", re.I)
RE_TIMED = re.compile(r"\d\s*(?:sek\b|s\b|s/|meter\b|–\d+s)|utmattning|så långt|lugnt tempo", re.I)


def _code_for(name: str) -> str | None:
    key = name.casefold().strip()
    for k, v in NAME_TO_CODE.items():
        if key.startswith(k):
            return v
    return None


def _clean_load(load: str) -> str | None:
    return load.strip().rstrip(".").strip() or None


def parse_numbered(details: str) -> list[dict]:
    """'1) Namn · 3 set × 10 reps · kroppsvikt — note. 2) …' → lista."""
    block = details.split("\n\n", 1)[0]
    block = re.sub(r"^ÖVNINGAR[^.]*\.\s*", "", block)
    out = []
    for part in re.split(r"\s*\d+\)\s+", block):
        part = part.strip()
        if not part:
            continue
        head, _, note = part.partition(" — ")
        fields = [f.strip() for f in head.split(" · ")]
        name = fields[0]
        spec = fields[1] if len(fields) > 1 else ""
        load = fields[2] if len(fields) > 2 else ""
        timed = bool(RE_TIMED.search(spec))
        sets = reps = None
        m = RE_SETS_REPS.search(spec)
        if m and not timed:
            sets, reps = int(m.group(1)), int(m.group(2))
        else:
            m2 = RE_SETS_ONLY.search(spec)
            if m2:
                sets = int(m2.group(1))
        rir = RE_RIR.search(part)
        rest = RE_REST.search(part)
        # Spec:en följer med i noten när den bär mer än set×reps.
        keep_spec = timed or reps is None or "per" in spec
        note_parts = [p for p in ((spec if keep_spec else ""), note.rstrip(".")) if p]
        out.append({
            "code": _code_for(name), "name": name,
            "sets": sets, "reps": reps,
            "rir": int(rir.group(1)) if rir else None,
            "rest_sec": int(rest.group(1)) if rest else None,
            "load": _clean_load(load),
            "note": " — ".join(note_parts) or None,
        })
    return out


def parse_bullets(details: str) -> list[dict]:
    """'- **Namn:** 2×15, RIR 4, 60s vila' + '   - _note_' → lista."""
    out: list[dict | None] = []
    skip = {"uppvärmning", "nedvarvning"}
    for line in details.splitlines():
        m = re.match(r"^- \*\*(.+?):\*\*\s*(.*)$", line)
        if m:
            name, spec = m.group(1), m.group(2)
            if name.casefold() in skip:
                out.append(None)
                continue
            first = spec.split(",", 1)[0].strip()          # "2×15", "2×12/ben", "2×30–45s/ben"
            timed = bool(RE_TIMED.search(first))
            sr = RE_SETS_REPS.search(first)
            sets = reps = None
            if sr and not timed:
                sets, reps = int(sr.group(1)), int(sr.group(2))
            elif sr:
                sets = int(sr.group(1))
            rir = RE_RIR.search(spec)
            rest = RE_REST.search(spec)
            note = None
            if timed:
                note = first                                  # "2×30–45s/ben"
            elif sr and first != sr.group(0):
                note = f"{reps}{first[len(sr.group(0)):]}"    # "12/ben"
            out.append({
                "code": _code_for(name), "name": name,
                "sets": sets, "reps": reps,
                "rir": int(rir.group(1)) if rir else None,
                "rest_sec": int(rest.group(1)) if rest else None,
                "load": "kroppsvikt", "note": note,
            })
            continue
        m = re.match(r"^\s+- _(.+)_\s*$", line)
        if m and out and out[-1] is not None:
            prev = out[-1]["note"]
            out[-1]["note"] = (prev + " — " if prev else "") + m.group(1)
    return [e for e in out if e is not None]


def build(row: dict) -> list[dict]:
    if row.get("steps"):
        template = WORKOUTS.get(row.get("workout_code") or "") or {}
        reps_range = (template.get("parameters") or {}).get("reps")
        return exercises_from_steps(row["steps"], CATALOGUE, reps_range)
    details = row.get("details") or ""
    if details.startswith("ÖVNINGAR"):
        return normalize_exercises(parse_numbered(details))
    if "- **" in details:
        return normalize_exercises(parse_bullets(details))
    return []


def main() -> None:
    write = "--write" in sys.argv
    client = get_postgrest()
    rows = (
        client.table("planned_sessions")
        .select("id, date, title, origin, workout_code, steps, details")
        .eq("user_id", USER).eq("sport", "Styrka").is_("exercises", "null")
        .gte("date", "2026-08-26").order("date").execute()
    ).data or []
    for row in rows:
        exercises = build(row)
        print(f"\n=== {row['date']} {row['title']} ({row['origin']}) → {len(exercises)} övningar")
        for ex in exercises:
            slim = {k: v for k, v in ex.items() if v is not None}
            print("  " + json.dumps(slim, ensure_ascii=False))
        if write and exercises:
            client.table("planned_sessions").update({"exercises": exercises}).eq("id", row["id"]).execute()
            print("  ✓ skrivet")


if __name__ == "__main__":
    main()
