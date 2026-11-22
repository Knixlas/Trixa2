"""Passbankens adepttext är svenska utan fackförkortningar (docs/PASSBANK_SPRAK.md).

En nybörjare fick "Pickups är gradvisa fartökningar … Skillnad mot strides",
"HR driftar över Z2" och "IM-maratonstrategi" (docs/13). Testet läser bara
fälten adepten ser; tränarens noter och katalogfält får vara fackspråk.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKOUTS = Path(__file__).resolve().parents[1] / "data" / "workouts"

# Fält som renderas för adepten (coach/engine/renderer.py).
_WORKOUT_FIELDS = ("name", "intent")
_SEGMENT_FIELDS = ("description", "effort_descriptor", "note")

from coach.engine.sprak import FORBIDDEN as _FORBIDDEN  # noqa: E402
from coach.engine.sprak import PAREN_ONLY as _PAREN_ONLY  # noqa: E402
from coach.engine.sprak import jargon as _jargon  # noqa: E402
from coach.engine.sprak import outside_parens as _outside_parens  # noqa: E402


def _texts():
    for path in sorted(WORKOUTS.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if path.name == "swim_drills.yaml":
            for d in data.get("drills", []):
                yield path.name, d.get("code"), "name", d.get("name") or ""
                yield path.name, d.get("code"), "intent", (d.get("intent") or "").strip().split("\n")[0]
            continue
        if path.name == "strength_exercises.yaml":
            for e in data.get("exercises", []):
                yield path.name, e.get("code"), "name", e.get("name") or ""
            continue
        for w in data.get("workouts", []):
            code = w.get("code")
            for f in _WORKOUT_FIELDS:
                yield path.name, code, f, str(w.get(f) or "")
            for seg in w.get("main_set") or []:
                for f in _SEGMENT_FIELDS:
                    if seg.get(f):
                        yield path.name, code, f, str(seg[f])
            for cond in w.get("abort_conditions") or []:
                yield path.name, code, "abort_conditions", str(cond)


def _hits() -> list[str]:
    return [
        f"{fname} {code} {field}: {m!r}"
        for fname, code, field, text in _texts()
        for m in _jargon(text)
    ]


def test_adepttexten_saknar_fackforkortningar():
    hits = _hits()
    assert not hits, f"{len(hits)} träffar, de första:\n" + "\n".join(hits[:40])


@pytest.mark.parametrize("pat", _FORBIDDEN)
def test_monstren_ar_giltiga(pat):
    re.compile(pat)
