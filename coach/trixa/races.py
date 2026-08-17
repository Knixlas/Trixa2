"""Tävlingskalender — läs adeptens races från public.races.

Ersätter coach/data/races.yaml (Niklas-specifik data i generell fil,
borttagen 2026-07-02). Planner räknar fas från nästa A-race; B/C används
som fallback om inget A-race finns framåt i tiden.
"""

from __future__ import annotations

from datetime import date
from typing import Any


_PRIORITY_ORDER = {"A": 0, "B": 1, "C": 2}


def fetch_upcoming_races(client: Any, athlete_id: str, today: date) -> list[dict]:
    """Alla kommande races för adepten, sorterade prio (A först) + datum."""
    res = (
        client.table("races")
        .select("*")
        .eq("athlete_id", athlete_id)
        .gte("date", today.isoformat())
        .order("date")
        .execute()
    )
    rows = res.data or []
    return sorted(
        rows,
        key=lambda r: (_PRIORITY_ORDER.get(r.get("priority"), 9), r.get("date", "")),
    )


def fetch_last_race(client: Any, athlete_id: str, today: date) -> dict | None:
    """Senast genomförda race (date < today), oavsett prioritet.

    Används för transition-logiken: engine går in i återhämtningsfas om ett
    lopp genomförts nyligen (last_race_completed_within_days).
    """
    res = (
        client.table("races")
        .select("*")
        .eq("athlete_id", athlete_id)
        .lt("date", today.isoformat())
        .order("date", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def fetch_next_a_race(client: Any, athlete_id: str, today: date) -> dict | None:
    """Nästa A-race (närmast i datum). Fallback: närmaste B, sedan C.

    Returnerar races-raden eller None om kalendern är tom framåt.
    """
    upcoming = fetch_upcoming_races(client, athlete_id, today)
    if not upcoming:
        return None
    for prio in ("A", "B", "C"):
        candidates = [r for r in upcoming if r.get("priority") == prio]
        if candidates:
            return min(candidates, key=lambda r: r.get("date", ""))
    return upcoming[0]
