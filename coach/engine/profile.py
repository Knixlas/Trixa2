"""Ladda AthleteProfile från olika källor med prioritetsordning.

Källor i prioritetsordning:
1. athlete_config.yaml (sanning — du redigerar manuellt vid testresultat)
2. garmin_coach.athlete_profile (fallback — auto-synkat från Garmin)
3. DEMO_PROFILE (sista utvägen — för att rendering inte ska krascha)

Använd `load_profile()` för "ge mig en profil, fixa det själv".
Använd `load_profile_from_yaml()` eller `load_profile_from_supabase()`
för explicit kontroll.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from .loader import AthleteProfile


# Default-sökväg för athlete_config.yaml — i coach/data/, syskon till workouts/.
# Från coach/engine/profile.py: parent.parent = coach/.
DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "athlete_config.yaml"
)


# Sista-utvägs-profil. Inte tänkt att användas i produktion.
# Värden som matchar Niklas första uppskattningar (2026-05-23).
DEMO_PROFILE = AthleteProfile(
    css_sec_per_100m=135.0,
    ftp_watts=198,
    lthr_bike_bpm=160,
    threshold_pace_sec_per_km=315.0,
    at_hr_run_bpm=170,
    max_hr_bpm=185,
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
) -> AthleteProfile:
    """Ladda profil från första källa som lyckas.

    Prioritet:
    1. athlete_config.yaml (om filen finns)
    2. Supabase (om query + garmin_athlete_id getts)
    3. DEMO_PROFILE (sista utväg, med varning)

    Args:
        config_path: override för YAML-sökväg
        supabase_query: callable för DB-anrop (samma som adapters/garmin.py)
        garmin_athlete_id: UUID för Supabase-raden
        verbose: skriv vilken källa som användes

    Returns:
        AthleteProfile redo att skickas till renderaren.
    """
    # 1. YAML
    try:
        profile = load_profile_from_yaml(config_path)
        if verbose:
            print(f"  Källa: athlete_config.yaml")
        return profile
    except ProfileSourceError as exc:
        if verbose:
            print(f"  athlete_config.yaml: {exc}")

    # 2. Supabase
    if supabase_query is not None and garmin_athlete_id is not None:
        try:
            profile = load_profile_from_supabase(garmin_athlete_id, supabase_query)
            if verbose:
                print(f"  Källa: Supabase (athlete_id={garmin_athlete_id})")
            return profile
        except ProfileSourceError as exc:
            if verbose:
                print(f"  Supabase: {exc}")

    # 3. Demo
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
