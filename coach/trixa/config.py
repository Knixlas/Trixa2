"""Driftinställningar som förut låg som literaler på fem ställen.

``DEFAULT_USER_ID = "09db449d-…"`` (Niklas) fanns i sync_week,
structure_sessions, run_sync, push_week och ui — bara ui läste miljön.
Att sätta TRIXA_DEFAULT_USER_ID på Railway bytte alltså vem webben låtsades
vara, medan varje CLI fortsatte synka och pusha åt Niklas (docs/12 I3).

Ingen literal här. Saknas variabeln finns ingen default, och den som kör
ett CLI utan ``--user`` får ett fel i stället för någon annans plan.
"""

from __future__ import annotations

import os


def default_user_id() -> str | None:
    """profiles.id för dev-escape och CLI-default. None om ej konfigurerad."""
    return (os.environ.get("TRIXA_DEFAULT_USER_ID") or "").strip() or None


def default_athlete_id() -> str | None:
    """garmin_coach.athlete_profile.id (recovery-nyckel) för CLI-default."""
    return (os.environ.get("TRIXA_DEFAULT_ATHLETE_ID") or "").strip() or None


def require(value: str | None, flag: str, env: str) -> str:
    """Ett CLI-argument som måste finnas — antingen som flagga eller miljö."""
    if value:
        return value
    raise SystemExit(f"Ange {flag} eller sätt {env}.")
