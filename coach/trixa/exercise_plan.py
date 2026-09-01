"""Strukturerad övningslista på det planerade passet.

Ett styrkepass bar sitt innehåll på två ställen: renderad prosa i ``details``
och passbankens ``main_set`` i ``steps``. Loggformuläret (``exercise_logs``)
tar emot namn, set, reps, vikt och ansträngning — men hade ingen väg dit.
Adepten skrev in varje övningsnamn för hand, trots att Trixa visste exakt vad
passet innehöll. Ett benpass med tolv övningar blev tolv manuella inmatningar
av data appen själv genererat.

Den här modulen normaliserar ``strength_block``-stegen till en lista som både
planeringen (``planned_sessions.exercises``) och loggformuläret läser, så att
loggningen blir en bekräftelse i stället för en avskrift.

Att förifylla FORMULÄRET är rätt; att skriva loggraden åt adepten vore att
registrera pass som inte utförts, och det förstör just den datakvalitet motorn
vilar på. Ingen funktion här skriver till ``exercise_logs``.
"""

from __future__ import annotations

from typing import Any


def _scalar(value: Any) -> Any:
    """Passbankens tal kan vara mallar: {"range": [4, 10], "default": 6}."""
    if isinstance(value, dict):
        value = (
            value.get("default")
            or value.get("estimated")
            or (value.get("range") or [None])[0]
        )
    return value if isinstance(value, (int, float, str)) or value is None else None


def _int_or_none(value: Any) -> int | None:
    value = _scalar(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rep_bounds(value: Any, fallback: Any = None) -> tuple[int | None, int | None]:
    """Repspannet ur en prescription, med passets mallspann som reserv.

    Dubbel progression behöver ett golv och ett tak, inte bara ett tal: utan
    spannet vet ingen när reps-ökningen ska växlas mot en tyngre stång.
    Spannet finns antingen i steget (``reps: {range: [3, 6]}``) eller på
    passets parameter (``parameters.reps.range``).
    """
    for candidate in (value, fallback):
        if isinstance(candidate, dict):
            span = candidate.get("range")
        else:
            span = candidate
        if isinstance(span, (list, tuple)) and len(span) >= 2:
            low, high = _int_or_none(span[0]), _int_or_none(span[1])
            if low and high and high >= low:
                return low, high
    return None, None


def readable_name(code: str) -> str:
    """Kod → läsbart namn när katalogen saknar övningen."""
    return (code or "").replace("_", " ").strip().capitalize() or "Övning"


def exercises_from_steps(
    steps: Any,
    exercise_map: dict[str, dict] | None = None,
    reps_range: Any = None,
) -> list[dict]:
    """``main_set``-steg → övningslista i den form loggformuläret vill ha.

    Bara ``strength_block`` blir övningar. Uppvärmning och nedvarvning hör till
    passtexten, inte till loggen — de har inga set och reps att bekräfta.

    ``reps_range`` är passets mallspann (``parameters.reps``) och används som
    reserv när steget bara bär ett rep-tal. Spannet följer med posten så att
    progressionen vet när reps ska växlas mot vikt.
    """
    catalogue = exercise_map or {}
    out: list[dict] = []
    for step in steps or []:
        if not isinstance(step, dict) or step.get("segment") != "strength_block":
            continue
        code = str(step.get("exercise") or "").strip()
        prescription = step.get("prescription") or {}
        if not isinstance(prescription, dict):
            prescription = {}
        entry = catalogue.get(code) or {}
        reps_min, reps_max = _rep_bounds(prescription.get("reps"), reps_range)
        out.append({
            "code": code or None,
            "name": entry.get("name") or readable_name(code),
            "sets": _int_or_none(prescription.get("sets")),
            "reps": _int_or_none(prescription.get("reps")),
            "reps_min": reps_min,
            "reps_max": reps_max,
            "rir": _int_or_none(prescription.get("rir")),
            "rest_sec": _int_or_none(prescription.get("rest_sec")),
            "load": _scalar(step.get("load_pct")),
            "alt": (str(step.get("alt")).strip() or None) if step.get("alt") else None,
            "note": (str(step.get("note")).strip() or None) if step.get("note") else None,
        })
    return out


def normalize_exercises(raw: Any) -> list[dict]:
    """Övningar från en extern skrivare (AI-coach) → samma form som ovan.

    Fritt formade objekt in, känd form ut. Ett namn är minimikravet — utan det
    finns inget att bocka av i loggen, och raden hör hemma i passtexten i
    stället.
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        weight = _scalar(item.get("weight_from"))
        try:
            weight = float(weight) if weight not in (None, "") else None
        except (TypeError, ValueError):
            weight = None
        reps_min, reps_max = _rep_bounds(
            item.get("reps"),
            [item.get("reps_min"), item.get("reps_max")]
            if item.get("reps_min") and item.get("reps_max") else None,
        )
        out.append({
            "code": (str(item.get("code")).strip() or None) if item.get("code") else None,
            "name": name[:80],
            "sets": _int_or_none(item.get("sets")),
            "reps": _int_or_none(item.get("reps")),
            "reps_min": reps_min,
            "reps_max": reps_max,
            "rir": _int_or_none(item.get("rir")),
            "rest_sec": _int_or_none(item.get("rest_sec")),
            "weight_from": weight,
            "load": _scalar(item.get("load")),
            "note": (str(item.get("note")).strip() or None) if item.get("note") else None,
        })
    return out
