"""Vem skrev raden i planned_sessions, och vad får hända med den.

``origin`` är 'trixa2' (motorn), 'nils' (coachen), 'manual' (adepten) eller
NULL (legacy). Policyn för varje värde låg som strängjämförelser på sex
ställen (docs/12 I13) — en facett här, en där — och inget enumererade
värdena tillsammans. En framtida skrivare ('tp', 'claude_mobile') hade
fått rätt behandling på några ställen och fel på andra, utan test.

Här är facetterna samlade. Regeln som allt annat vilar på: allt som INTE är
motorn räknas som människa, och människan vinner.
"""

from __future__ import annotations

ENGINE = "trixa2"
COACH = "nils"
ATHLETE = "manual"


def is_engine(origin: str | None) -> bool:
    return (origin or "") == ENGINE


def is_human(origin: str | None) -> bool:
    """Skyddad från regenerering; ett människoskrivet pass gäller."""
    return not is_engine(origin)


def reps_prescribed(origin: str | None) -> bool:
    """Rep-talet är en föreskrift (coach/adept) — inte en startpunkt som
    progressionen får flytta. Bara passbankens genererade pass autoregleras."""
    return is_human(origin)


def athlete_deletable(origin: str | None) -> bool:
    """Adepten får ta bort sina egna pass, inte coachens eller motorns."""
    return (origin or "") == ATHLETE


def swappable(origin: str | None, sport: str | None) -> bool:
    """"Byt pass"/"byt gren" gäller motorns pass; vila och brick har inga alternativ."""
    return is_engine(origin) and (sport or "") not in ("rest", "brick")


def plan_source(rows: list[dict]) -> str:
    """Vem som byggt veckan: 'coach', 'engine' eller 'mixed'."""
    has_coach = any((r.get("origin") or "") == COACH for r in rows)
    has_engine = any(is_engine(r.get("origin")) for r in rows)
    if has_coach and has_engine:
        return "mixed"
    return "coach" if has_coach else "engine"
