"""Passbankens språkregler som kod (docs/PASSBANK_SPRAK.md).

Delas av lint-testet för passbanken och av servern som prövar AI-coachers
passförslag (trixa_api/agent_passbank.py), så att samma ord stoppas båda
vägarna in.
"""

from __future__ import annotations

import re

FORBIDDEN = [
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
PAREN_ONLY = [r"\bFTP\b", r"\bCSS\b"]



_PARAM = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def outside_parens(text: str) -> str:
    return re.sub(r"\([^()]*\)", "", text)


def jargon(text: str) -> list[str]:
    """Fackord och koder i en text adepten ska läsa. Tom lista = godkänd."""
    text = _PARAM.sub("", text or "")
    out: list[str] = []
    for pat in FORBIDDEN:
        out.extend(re.findall(pat, text, flags=re.IGNORECASE if pat.islower() else 0))
    bare = outside_parens(text)
    for pat in PAREN_ONLY:
        out.extend(f"{m} utanför parentes" for m in re.findall(pat, bare))
    return out
