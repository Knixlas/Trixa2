"""Deterministisk mappning: Nils fritext-session → passbankskod.

Nils planerar i `planned_sessions` med fritext (titel + sport + duration), ofta
utan `workout_code`/`steps`. Trixa får inte tolka fritext med en LLM — men en
regeltabell (`data/session_mapping.yaml`) är deterministisk och inspekterbar.

Den här modulen laddar reglerna och exponerar `resolve_session()` som väljer en
passbankskod ur titeln. Stegupplösning + DB-skrivning sker i
`coach.trixa.structure_sessions` (separation: "vilken kod" vs "fyll steps").

Princip (Trixa): gissa inte. Matchar ingen regel → returnera None, så att
anroparen kan eskalera (alert) i stället för att fabricera ett pass.
"""

from __future__ import annotations

from typing import Any

from coach.engine._loader import load_yaml

# Nils sport-etiketter (svenska) → passbank-disciplin.
SPORT_TO_DISCIPLINE = {
    "Sim": "swim", "Simning": "swim",
    "Cykel": "bike", "Cykling": "bike",
    "Löpning": "run", "Lopning": "run", "Löp": "run",
    "Styrka": "strength",
    "Vila": "rest", "Brick": "brick",
}


def discipline_for_sport(sport: str | None) -> str:
    """Översätt en (svensk) sport-etikett till passbank-disciplin."""
    if not sport:
        return ""
    return SPORT_TO_DISCIPLINE.get(sport, str(sport).strip().lower())


def load_session_mapping(data_dir: Any = None) -> list[dict]:
    """Läs reglerna ur session_mapping.yaml (lista, i prövningsordning)."""
    data = load_yaml("session_mapping.yaml", data_dir) if data_dir else load_yaml("session_mapping.yaml")
    return list(data.get("rules", []))


def _duration_range(workout: dict) -> tuple[float, float] | None:
    """Hämta (min, max) för ett pass duration-range, om det finns."""
    p = (workout.get("parameters") or {}).get("duration_min")
    if isinstance(p, dict):
        rng = p.get("range")
        if isinstance(rng, list) and len(rng) == 2:
            try:
                return float(rng[0]), float(rng[1])
            except (TypeError, ValueError):
                return None
        # vissa pass har {min, max} i stället för range
        if "min" in p and "max" in p:
            try:
                return float(p["min"]), float(p["max"])
            except (TypeError, ValueError):
                return None
    return None


def _duration_default(workout: dict) -> float:
    p = (workout.get("parameters") or {}).get("duration_min")
    if isinstance(p, dict) and p.get("default") is not None:
        try:
            return float(p["default"])
        except (TypeError, ValueError):
            pass
    est = (workout.get("total_duration_min") or {})
    if isinstance(est, dict) and isinstance(est.get("estimated"), (int, float)):
        return float(est["estimated"])
    return 60.0


def _pick_by_duration(codes: list[str], duration_min: float, pool: dict) -> str | None:
    """Välj koden vars duration-range rymmer duration_min; annars närmast default."""
    existing = [(c, pool[c]) for c in codes if c in pool]
    if not existing:
        return None
    for c, w in existing:
        rng = _duration_range(w)
        if rng and rng[0] <= duration_min <= rng[1]:
            return c
    return min(existing, key=lambda cw: abs(_duration_default(cw[1]) - duration_min))[0]


def _rule_matches(rule: dict, discipline: str, title_lc: str) -> bool:
    if rule.get("sport") != discipline:
        return False
    any_kw = rule.get("any_keywords") or []
    if any_kw and not any(k.lower() in title_lc for k in any_kw):
        return False
    all_kw = rule.get("keywords") or []
    if all_kw and not all(k.lower() in title_lc for k in all_kw):
        return False
    exclude = rule.get("exclude") or []
    if exclude and any(k.lower() in title_lc for k in exclude):
        return False
    return True


def resolve_session(
    sport: str | None,
    title: str | None,
    duration_min: float,
    pool: dict,
    rules: list[dict] | None = None,
) -> tuple[str | None, str | None]:
    """Välj en passbankskod för en fritext-session.

    Args:
        sport: Nils sport-etikett ("Cykel", "Löpning", "Sim", ...).
        title: fritext-titeln.
        duration_min: planerad längd (styr val inom en regels kandidatlista).
        pool: {code: workout} (från load_workouts()).
        rules: regellista; laddas från YAML om None.

    Returns:
        (code, source) där source = "rule" | None. (None, None) om ingen regel
        matchar — anroparen ska eskalera, inte gissa.
    """
    discipline = discipline_for_sport(sport)
    if discipline in ("rest", "", "brick", "strength"):
        return None, None
    if rules is None:
        rules = load_session_mapping()
    title_lc = (title or "").lower()
    for rule in rules:
        if _rule_matches(rule, discipline, title_lc):
            code = _pick_by_duration(rule.get("codes") or [], duration_min, pool)
            if code:
                return code, "rule"
    return None, None
