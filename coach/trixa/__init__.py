"""Trixa-skikt — deterministisk veckoplanering ovanpå engine + passbank.

Trixa är publika tränaren (Trixa). LLM-coachen Nils läser samma data men kör i
ett eget skikt (Claude). Trixa skriver veckoplan, Nils kan göra override.

Moduler:
    db        — Supabase-klient-wrapper
    planner   — `generate_week(athlete_user_id, week_start)`. Huvudentry.
    selector  — välj konkreta pass från passbanken med variation
    scheduler — fördela pass över veckodagar
"""
