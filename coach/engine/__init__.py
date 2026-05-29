"""Passbank — läsning, validering och rendering av träningspass.

Modulerna jobbar tillsammans:
- loader: läser YAML-filer från coach/data/workouts/
- validator: kontrollerar schema, unicitet, referenser
- zones: beräknar adept-specifika zoner per disciplin
- renderer: producerar människoläsbar prosa per pass
- templates: hanterar parameterized templates (uttrycksevaluering)

Använd `verify_and_render.py` som CLI-entry-point.
"""

from .loader import load_workouts, load_drills, AthleteProfile
from .profile import load_profile, load_profile_from_yaml, load_profile_from_supabase
from .validator import validate_passbank, ValidationError
from .zones import compute_zones, ZoneSet
from .renderer import render_workout
from .templates import resolve_template

__all__ = [
    "load_workouts",
    "load_drills",
    "AthleteProfile",
    "load_profile",
    "load_profile_from_yaml",
    "load_profile_from_supabase",
    "validate_passbank",
    "ValidationError",
    "compute_zones",
    "ZoneSet",
    "render_workout",
    "resolve_template",
]
