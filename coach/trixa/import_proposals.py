"""Lägg in godkända passförslag i passbankens YAML.

    python -m coach.trixa.import_proposals            # visar vad som skulle hända
    python -m coach.trixa.import_proposals --apply    # skriver YAML + status imported

Förslagen kommer från AI-coacher via MCP (``workout_proposals``, migration
014) och har godkänts i ``/ui/admin/forslag``. Passbanken är granskad kod i
git: skriptet skriver in passet i rätt fil, ger det en riktig passkod,
kör validatorn och språkkontrollen och återställer filen om något fallerar.
Därefter: kör testerna, committa och öppna en PR som vanligt. Status sätts
till ``imported`` först när filen faktiskt skrivits.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

WORKOUTS = Path(__file__).resolve().parents[1] / "data" / "workouts"


def _target_file(workout: dict) -> Path | None:
    sport, category = workout["discipline"], workout["category"]
    if sport == "brick":
        name = "brick_BW.yaml"
    elif sport == "strength":
        name = f"strength_{workout.get('type_code') or 'AA'}.yaml"
    else:
        name = f"{sport}_{category}.yaml"
    path = WORKOUTS / name
    return path if path.exists() else None


def _next_code(workout: dict, existing: set[str]) -> str:
    prefix = f"{workout.get('type_code') or workout['category']}_{workout['discipline']}_"
    numbers = [
        int(m.group(1)) for code in existing
        if code.startswith(prefix) and (m := re.search(r"_(\d+)$", code))
    ]
    return f"{prefix}{max(numbers, default=0) + 1:02d}"


def _yaml_block(workout: dict, proposal: dict) -> str:
    ev = proposal.get("evidence") or {}
    again = ev.get("again") or {}
    workout = dict(workout)
    workout["coach_notes"] = (
        f"Föreslaget av {proposal.get('proposer_name') or 'AI-coach'} "
        f"{str(proposal.get('created_at') or '')[:10]} (förslag {proposal['id']}). "
        f"{(proposal.get('rationale') or '').strip()} "
        f"Underlag: {ev.get('done', 0)} av {ev.get('sessions', 0)} källpass gjorda, "
        f"vill göra igen ja {again.get('yes', 0)} / kanske {again.get('maybe', 0)} / "
        f"nej {again.get('no', 0)}."
    ).strip() + "\n"
    dumped = yaml.safe_dump([workout], allow_unicode=True, sort_keys=False, width=88)
    indented = "\n".join(("  " + line) if line else line for line in dumped.splitlines())
    return (
        f"\n  # Från passförslag {proposal['id']}, godkänt "
        f"{str(proposal.get('reviewed_at') or '')[:10]}\n{indented}\n"
    )


def _validate() -> list[str]:
    from coach.engine.loader import load_drills, load_workouts
    from coach.engine.sprak import jargon
    from coach.engine.validator import ValidationError, validate_passbank

    errors: list[str] = []
    try:
        validate_passbank(load_workouts(), load_drills())
    except ValidationError as exc:
        errors.extend(exc.errors)
    for w in load_workouts():
        for text in [w.get("name") or "", w.get("intent") or ""] + [
            str(seg.get(f)) for seg in w.get("main_set") or []
            for f in ("description", "effort_descriptor", "note") if seg.get(f)
        ] + [str(c) for c in w.get("abort_conditions") or []]:
            if jargon(text):
                errors.append(f"{w.get('code')}: fackord {jargon(text)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    from coach.engine.loader import load_workouts
    from coach.trixa.db import get_postgrest

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="skriv YAML och sätt status imported")
    args = parser.parse_args(argv)

    client = get_postgrest()
    proposals = (
        client.table("workout_proposals").select("*")
        .eq("status", "accepted").order("reviewed_at").execute()
    ).data or []
    if not proposals:
        print("Inga godkända förslag att lägga in.")
        return 0

    existing = {w["code"] for w in load_workouts()}
    failures = 0
    for p in proposals:
        workout = dict(p["workout"])
        target = _target_file(workout)
        if target is None:
            print(f"✗ {p['name']}: ingen passbanksfil för {workout['discipline']}/{workout['category']}")
            failures += 1
            continue
        code = _next_code(workout, existing)
        workout["code"] = code
        block = _yaml_block(workout, p)
        print(f"{'→' if args.apply else '·'} {p['name']} → {target.name} som {code}")
        if not args.apply:
            continue
        before = target.read_text(encoding="utf-8")
        target.write_text(before.rstrip("\n") + "\n" + block, encoding="utf-8")
        errors = _validate()
        if errors:
            target.write_text(before, encoding="utf-8")
            print("  ✗ validering föll, filen återställd:\n    " + "\n    ".join(errors[:10]))
            failures += 1
            continue
        existing.add(code)
        client.table("workout_proposals").update({
            "status": "imported", "imported_code": code,
        }).eq("id", p["id"]).eq("status", "accepted").execute()
        print(f"  ✓ inlagt {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    if args.apply and failures == 0:
        print("\nKör testerna (pytest -q) och öppna en PR med YAML-ändringen.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
