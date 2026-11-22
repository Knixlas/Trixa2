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

_FORBIDDEN = [
    r"\bHR\b", r"\bbpm\b", r"\bpace\b", r"\bstrides?\b", r"\bpickups?\b",
    r"\bdrills?\b", r"\baerob\w*", r"\banaerob\w*", r"mitokondri\w*", r"kapillär\w*",
    r"kardiovaskulär\w*", r"\bdrift\w*", r"\bRIR\b", r"\bRPE\b", r"\bERG\b", r"\bIM\b",
    r"\brace\w*", r"\bpeak\b", r"\brecovery\b", r"\bcool-?down\b", r"\bwarm-?up\b",
    r"\bkick\b", r"\breps?\b", r"\bset\b", r"\bkadens\w*", r"\brpm\b", r"\bsighting\b",
    r"\bVO2\w*", r"sweet ?spot", r"negative split", r"\bsplits?\b", r"\bbase-?fas\w*",
    r"\bT[12]\b", r"\bOW\b", r"\bbrick\w*", r"\badepten?\b", r"\blaktat\w*",
    # fältnamn och koder (threshold_pace_sec_per_km, landmine_press …)
    r"\b[a-z]+_[a-z_]+\b",
]
# FTP och CSS får stå inom parentes, efter en förklaring.
_PAREN_ONLY = [r"\bFTP\b", r"\bCSS\b"]


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


def _outside_parens(text: str) -> str:
    return re.sub(r"\([^()]*\)", "", text)


def _hits() -> list[str]:
    out = []
    for fname, code, field, text in _texts():
        # {jog_min} m.fl. ersätts av renderaren och syns aldrig som de står.
        text = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", text)
        for pat in _FORBIDDEN:
            for m in re.findall(pat, text, flags=re.IGNORECASE if pat.islower() else 0):
                out.append(f"{fname} {code} {field}: {m!r}")
        bare = _outside_parens(text)
        for pat in _PAREN_ONLY:
            for m in re.findall(pat, bare):
                out.append(f"{fname} {code} {field}: {m!r} utanför parentes")
    return out


def test_adepttexten_saknar_fackforkortningar():
    hits = _hits()
    assert not hits, f"{len(hits)} träffar, de första:\n" + "\n".join(hits[:40])


@pytest.mark.parametrize("pat", _FORBIDDEN)
def test_monstren_ar_giltiga(pat):
    re.compile(pat)
