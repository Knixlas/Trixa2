"""Ladda AthleteProfile från olika källor med prioritetsordning.

Källor i prioritetsordning (multi-tenant sedan 2026-07-02):
1. public.athlete_profiles per user_id (sanning — per adept i Supabase)
2. athlete_config.example.yaml (endast dev/test, via explicit config_path)
3. garmin_coach.athlete_profile (fallback — auto-synkat från Garmin/TP)
4. DEMO_PROFILE (sista utvägen — för att rendering inte ska krascha)

Använd `load_profile()` för "ge mig en profil, fixa det själv".
Använd `profile_from_athlete_row()` när du redan har athlete_profiles-raden
(planner gör det) — den är den enda parsningsvägen för DB-radens textformat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from .loader import AthleteProfile


# Dev-fixture — generiska exempelvärden, INTE någon riktig adept.
# Riktiga profiler bor i public.athlete_profiles (en rad per användare).
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "athlete_config.example.yaml"
)


# Sista-utvägs-profil med neutrala mittfälts-värden för en medioker
# åldersgruppstriatlet. Inte tänkt att användas i produktion — bara för
# att rendering aldrig ska krascha när alla källor saknas.
DEMO_PROFILE = AthleteProfile(
    css_sec_per_100m=120.0,   # 2:00/100m
    ftp_watts=220,
    lthr_bike_bpm=158,
    threshold_pace_sec_per_km=300.0,  # 5:00/km
    at_hr_run_bpm=165,
    max_hr_bpm=180,
)


# (sql, params) -> list[dict]. Samma signatur som adapters/garmin.py använder.
QueryFn = Callable[[str, dict], list[dict]]


class ProfileSourceError(Exception):
    """Källan kunde inte läsas (fil saknas, DB-fel, etc.)."""


# ---------- YAML-källa ----------


def load_profile_from_yaml(
    config_path: Path | None = None,
) -> AthleteProfile:
    """Läs athlete_config.yaml och bygg en AthleteProfile.

    Mappar fältnamn från config-filen till AthleteProfile:
    - thresholds.css_sec_per_100m  →  css_sec_per_100m
    - thresholds.ftp_watts          →  ftp_watts
    - thresholds.threshold_pace_run_sec_per_km  →  threshold_pace_sec_per_km
    - thresholds.threshold_hr_run   →  at_hr_run_bpm
    - thresholds.max_hr             →  max_hr_bpm
    - (LTHR-bike saknas i nuvarande config — sätts till None)

    Raises:
        ProfileSourceError: om filen inte finns eller är ogiltig.
    """
    config_path = config_path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ProfileSourceError(f"Konfigurationsfilen saknas: {config_path}")

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileSourceError(f"Ogiltig YAML i {config_path}: {exc}") from exc

    if not data or "thresholds" not in data:
        raise ProfileSourceError(
            f"Saknar thresholds-sektion i {config_path}"
        )

    th = data["thresholds"]

    return AthleteProfile(
        css_sec_per_100m=_as_float(th.get("css_sec_per_100m")),
        ftp_watts=_as_int(th.get("ftp_watts")),
        lthr_bike_bpm=_as_int(th.get("lthr_bike")),
        threshold_pace_sec_per_km=_as_float(th.get("threshold_pace_run_sec_per_km")),
        at_hr_run_bpm=_as_int(th.get("threshold_hr_run")),
        max_hr_bpm=_as_int(th.get("max_hr")),
    )


# ---------- athlete_profiles-rad (MASTER per-användare-källa) ----------


def _parse_min_sec(value: Any) -> float | None:
    """'2:15' → 135.0. Tolererar redan-numeriska värden och None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parts = str(value).split(":")
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(value)
    except (ValueError, TypeError):
        return None


def profile_from_athlete_row(athlete: dict) -> AthleteProfile:
    """Översätt en public.athlete_profiles-rad → AthleteProfile.

    Enda parsningsvägen för radens textformat (swim_css '2:15',
    run_threshold_pace '5:15'). Kolumnerna max_hr/resting_hr/lthr_bike
    tillkom i migration 008 — saknas de (äldre rad) blir fälten None
    och renderaren utelämnar motsvarande zonvärden.
    """
    return AthleteProfile(
        css_sec_per_100m=_parse_min_sec(athlete.get("swim_css")),
        ftp_watts=_as_int(athlete.get("ftp")),
        lthr_bike_bpm=_as_int(athlete.get("lthr_bike")),
        threshold_pace_sec_per_km=_parse_min_sec(athlete.get("run_threshold_pace")),
        at_hr_run_bpm=_as_int(athlete.get("lthr")),
        max_hr_bpm=_as_int(athlete.get("max_hr")),
    )


def load_profile_from_athlete_profiles(
    athlete_user_id: str,
    client: Any,
) -> AthleteProfile:
    """Läs public.athlete_profiles per user_id (postgrest-klient).

    Raises:
        ProfileSourceError: om raden saknas eller saknar tröskelvärden.
    """
    try:
        res = (
            client.table("athlete_profiles")
            .select("ftp, lthr, lthr_bike, max_hr, swim_css, run_threshold_pace")
            .eq("user_id", athlete_user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise ProfileSourceError(f"athlete_profiles-läsning misslyckades: {exc}") from exc

    if not res.data:
        raise ProfileSourceError(
            f"Ingen athlete_profiles-rad för user_id {athlete_user_id}"
        )

    profile = profile_from_athlete_row(res.data[0])
    if profile.ftp_watts is None and profile.css_sec_per_100m is None and (
        profile.threshold_pace_sec_per_km is None
    ):
        raise ProfileSourceError(
            f"athlete_profiles-raden för {athlete_user_id} saknar alla tröskelvärden"
        )
    return profile


# ---------- Supabase-källa ----------


def load_profile_from_supabase(
    garmin_athlete_id: str,
    query: QueryFn,
) -> AthleteProfile:
    """Läs garmin_coach.athlete_profile och bygg en AthleteProfile.

    Försöker först strukturerade kolumner. För värden som är NULL där,
    försöker raw_profile (Garmins ursprungliga JSON som ofta har mer
    data än sync extraherar till kolumner).

    Args:
        garmin_athlete_id: UUID för raden i athlete_profile
        query: callable för att utföra SQL (samma signatur som
            adapters/garmin.py — så samma DB-klient kan återanvändas)

    Raises:
        ProfileSourceError: om raden inte hittas eller är tom.
    """
    sql = """
        SELECT
            ftp_watts,
            lactate_threshold_hr,
            threshold_pace_run_sec_per_km,
            threshold_pace_swim_sec_per_100m,
            max_hr,
            resting_hr,
            raw_profile
        FROM garmin_coach.athlete_profile
        WHERE id = :athlete_id
    """
    rows = query(sql, {"athlete_id": garmin_athlete_id})
    if not rows:
        raise ProfileSourceError(
            f"Ingen rad i garmin_coach.athlete_profile med id {garmin_athlete_id}"
        )

    row = rows[0]
    raw = row.get("raw_profile") or {}
    user_data = raw.get("userData", {}) if isinstance(raw, dict) else {}

    # För varje fält: använd strukturerad kolumn om den finns, annars raw_profile.
    ftp = _as_int(row.get("ftp_watts"))
    # Garmin har ingen explicit FTP-watt i raw_profile vi sett, så bara kolumnen.

    # LTHR: strukturerad kolumn, fallback till userData.lactateThresholdHeartRate
    lthr = _as_int(row.get("lactate_threshold_hr"))
    if lthr is None:
        lthr = _as_int(user_data.get("lactateThresholdHeartRate"))

    # LTHR-bike: separat fält i raw, ofta null för cykel
    lthr_bike = _as_int(user_data.get("lactateThresholdHeartRateCycling"))
    if lthr_bike is None:
        # Om Garmin inte vet cykel-LTHR specifikt, använd den allmänna
        # som proxy (mindre exakt, men bättre än ingenting)
        lthr_bike = lthr

    # Threshold-pace löpning: strukturerad kolumn
    # OBS: raw_profile har lactateThresholdSpeed men enheten är oklar.
    # Garmin dokumenterar den inte tydligt — preliminära test antyder att
    # rimliga m/s-konverteringar ger orealistiska värden. Hoppar över raw-
    # fallback för pace tills enheten är verifierad mot ett kalibrerat fall.
    threshold_pace_run = _as_float(row.get("threshold_pace_run_sec_per_km"))

    # CSS-sim
    css = _as_float(row.get("threshold_pace_swim_sec_per_100m"))

    # Max-HR: strukturerad kolumn (raw har det också under userData.maxAvgHr
    # eller liknande, men varierande — håll oss till kolumnen)
    max_hr = _as_int(row.get("max_hr"))

    return AthleteProfile(
        css_sec_per_100m=css,
        ftp_watts=ftp,
        lthr_bike_bpm=lthr_bike,
        threshold_pace_sec_per_km=threshold_pace_run,
        at_hr_run_bpm=lthr,
        max_hr_bpm=max_hr,
    )


# ---------- Prioriterad fallback ----------


def load_profile(
    config_path: Path | None = None,
    supabase_query: QueryFn | None = None,
    garmin_athlete_id: str | None = None,
    verbose: bool = False,
    athlete_user_id: str | None = None,
    client: Any | None = None,
) -> AthleteProfile:
    """Ladda profil från första källa som lyckas.

    Prioritet:
    1. public.athlete_profiles (om athlete_user_id + postgrest-client getts)
    2. YAML-fixture (endast dev/test — example-filen eller explicit path)
    3. garmin_coach.athlete_profile (om query + garmin_athlete_id getts)
    4. DEMO_PROFILE (sista utväg, med varning)

    Args:
        config_path: override för YAML-sökväg (dev/test)
        supabase_query: callable för raw-SQL (samma som adapters/garmin.py)
        garmin_athlete_id: UUID för garmin_coach-raden
        athlete_user_id: profiles.id — MASTER-vägen per användare
        client: postgrest-klient (coach.trixa.db.get_supabase())
        verbose: skriv vilken källa som användes

    Returns:
        AthleteProfile redo att skickas till renderaren.
    """
    # 1. athlete_profiles per användare (MASTER)
    if athlete_user_id is not None and client is not None:
        try:
            profile = load_profile_from_athlete_profiles(athlete_user_id, client)
            if verbose:
                print(f"  Källa: athlete_profiles (user_id={athlete_user_id})")
            return profile
        except ProfileSourceError as exc:
            if verbose:
                print(f"  athlete_profiles: {exc}")

    # 2. YAML-fixture (dev/test)
    try:
        profile = load_profile_from_yaml(config_path)
        if verbose:
            print(f"  Källa: YAML-fixture ({config_path or DEFAULT_CONFIG_PATH})")
        return profile
    except ProfileSourceError as exc:
        if verbose:
            print(f"  YAML-fixture: {exc}")

    # 3. garmin_coach.athlete_profile
    if supabase_query is not None and garmin_athlete_id is not None:
        try:
            profile = load_profile_from_supabase(garmin_athlete_id, supabase_query)
            if verbose:
                print(f"  Källa: garmin_coach (athlete_id={garmin_athlete_id})")
            return profile
        except ProfileSourceError as exc:
            if verbose:
                print(f"  garmin_coach: {exc}")

    # 4. Demo
    if verbose:
        print("  Källa: DEMO_PROFILE (fallback — inga andra källor tillgängliga)")
    return DEMO_PROFILE


# ---------- Hjälpare ----------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
