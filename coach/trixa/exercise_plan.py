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

import re
from typing import Any

from coach.engine.numbers import to_int


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
    return to_int(_scalar(value))


def _rep_bounds(value: Any, fallback: Any = None) -> tuple[int | None, int | None]:
    """Repspannet ur en prescription, med passets mallspann som reserv.

    Dubbel progression behöver ett golv och ett tak, inte bara ett tal: utan
    spannet vet ingen när reps-ökningen ska växlas mot en tyngre stång.
    Spannet finns antingen i steget (``reps: {range: [3, 6]}``) eller på
    passets parameter (``parameters.reps.range``).

    Mallspannet gäller passets huvudlyft. Ett steg vars föreskrivna reps
    ligger utanför det (pallof-press 10 mot [5, 8], planka ``reps: 1`` mot
    [12, 15]) är en accessoar eller en hålltid — då är spannet fel för
    steget, och progressionen skulle tvinga "för tungt" på en 30-sekunders
    planka varje gång. Sådana steg får inget spann.
    """
    planned = _int_or_none(value)
    for candidate, is_fallback in ((value, False), (fallback, True)):
        if isinstance(candidate, dict):
            span = candidate.get("range")
        else:
            span = candidate
        if isinstance(span, (list, tuple)) and len(span) >= 2:
            low, high = _int_or_none(span[0]), _int_or_none(span[1])
            if low and high and high >= low:
                if is_fallback and planned is not None and not (low <= planned <= high):
                    return None, None
                return low, high
    return None, None


def planned_exercises(
    row: dict,
    exercise_map: dict[str, dict] | None = None,
    previous: dict | None = None,
) -> list[dict]:
    """Övningslistan för en planned_sessions-rad, oavsett hur den skrevs.

    Kedjan: ``exercises`` (sanningen) → ``steps`` (rader från före TX-4) →
    prosan i ``details`` (coachen skrev listan som text) → förra styrke-
    passets lista (``previous``; raden säger bara "samma som torsdag").
    Både adeptens app och coachens get_week går genom den här funktionen,
    så att de ser samma lista.

    De två sista stegen är reserver för pass som skrivits utan lista. De
    märks (``derived``) så att formuläret kan säga varifrån listan kommer
    — adepten ska veta att det är en tolkning, inte coachens ord.
    """
    if row.get("exercises"):
        return list(row["exercises"])
    from_steps = exercises_from_steps(row.get("steps"), exercise_map)
    if from_steps:
        return from_steps
    if (row.get("sport") or "").strip().casefold() not in ("styrka", "strength"):
        return []
    from_prose = exercises_from_prose(row.get("details"))
    if from_prose:
        return [{**e, "derived": "details"} for e in from_prose]
    if previous and previous.get("_exercises"):
        return [
            {**e, "derived": f"previous:{str(previous.get('date'))[:10]}"}
            for e in previous["_exercises"]
        ]
    return []


# Coachens kortnamn → passbankens kod. Bara där övningen är densamma.
_PROSE_CODE_ALIASES = {
    "benpress": "leg_press_machine",
    "bencurl": "leg_curl_machine",
    "liggande bencurl": "leg_curl_machine",
    "rodd": "seated_row_machine",
    "sittande rodd": "seated_row_machine",
    "pallof": "pallof_press",
    "pallof press": "pallof_press",
    "pallof-press": "pallof_press",
    "bröstpress": "chest_press_machine",
    "latsdrag": "lat_pulldown_machine",
    "axelpress": "shoulder_press_machine",
    "planka": "plank",
    "sidoplanka": "side_plank",
    "knäböj": "bodyweight_squat",
    "armhävning": "pushup",
    "armhävningar": "pushup",
    "höftlyft": "hip_thrust",
    "step-up": "step_up",
}

_RE_SETS_REPS = re.compile(r"(\d+)\s*(?:set)?\s*[×x]\s*(\d+)(?:\s*reps?)?", re.I)
_RE_SETS_ONLY = re.compile(r"^\s*(\d+)\s*set\b", re.I)
_RE_RIR = re.compile(r"RIR\s*(\d)", re.I)
_RE_REST = re.compile(r"(\d+)\s*s(?:ek)?\s*vila", re.I)
_RE_TIMED = re.compile(r"\d\s*(?:sek\b|s\b|s/|meter\b|–\d+s)|utmattning|så långt", re.I)
# "alla 2x10: benpress 83, höftabduktion 40, …" — set×reps för alla, vikt per övning.
_RE_ALL_PREFIX = re.compile(
    r"(?:alla|allt|samtliga)?\s*(\d+)\s*[×x]\s*(\d+)\s*:\s*([^\n.]+)", re.I
)
_RE_NAME_WEIGHT = re.compile(r"^\s*([^\d,]+?)\s+(\d+(?:[.,]\d+)?)\s*(?:kg)?\s*$")


def _code_for_prose_name(name: str) -> str | None:
    key = name.casefold().strip()
    if key in _PROSE_CODE_ALIASES:
        return _PROSE_CODE_ALIASES[key]
    for alias, code in _PROSE_CODE_ALIASES.items():
        if key.startswith(alias + " ") or key.startswith(alias + "("):
            return code
    return None


def exercises_from_prose(details: str | None) -> list[dict]:
    """Övningslista ur ett coachskrivet ``details`` — bara igenkända mönster.

    Coacher skriver listan som text trots att verktyget ber om en lista
    (docs/12 D-avsnittet, och igen 2026-09-10). Tre mönster täcker det som
    faktiskt förekommit:

    1. Numrerad: ``1) Benpress · 3 set × 10 reps · kroppsvikt — not.``
    2. Bullets: ``- **Knäböj:** 2×15, RIR 4, 60s vila``
    3. Gemensamt set×reps och vikt per övning:
       ``alla 2x10: benpress 83, höftabduktion 40, …``

    Tid/sträcka/"till utmattning" lämnar reps tomt. Allt annat ger tom lista
    hellre än en gissning — då faller läsaren tillbaka på förra passet eller
    fritextformuläret.
    """
    text = (details or "").strip()
    if not text:
        return []
    if text.startswith("ÖVNINGAR") or re.search(r"(?:^|\s)\d\)\s+\S", text):
        items = _parse_numbered(text)
    elif "- **" in text:
        items = _parse_bullets(text)
    else:
        items = _parse_all_prefix(text)
    return normalize_exercises(items)


def _parse_all_prefix(text: str) -> list[dict]:
    m = _RE_ALL_PREFIX.search(text)
    if not m:
        return []
    sets, reps = int(m.group(1)), int(m.group(2))
    out = []
    for part in m.group(3).split(","):
        nm = _RE_NAME_WEIGHT.match(part)
        if not nm:
            continue
        name = nm.group(1).strip()
        weight = float(nm.group(2).replace(",", "."))
        out.append({
            "code": _code_for_prose_name(name), "name": name[:1].upper() + name[1:],
            "sets": sets, "reps": reps, "weight_from": weight,
        })
    return out


def _parse_numbered(text: str) -> list[dict]:
    block = text.split("\n\n", 1)[0]
    block = re.sub(r"^ÖVNINGAR[^.]*\.\s*", "", block)
    out = []
    for part in re.split(r"\s*\d+\)\s+", block):
        part = part.strip()
        if not part:
            continue
        head, _, note = part.partition(" — ")
        fields = [f.strip() for f in head.split(" · ")]
        name = fields[0]
        spec = fields[1] if len(fields) > 1 else ""
        load = fields[2] if len(fields) > 2 else ""
        timed = bool(_RE_TIMED.search(spec))
        sets = reps = None
        sr = _RE_SETS_REPS.search(spec)
        if sr and not timed:
            sets, reps = int(sr.group(1)), int(sr.group(2))
        elif sr:
            sets = int(sr.group(1))
        else:
            so = _RE_SETS_ONLY.match(spec)      # "4 set × till nära utmattning"
            if so:
                sets = int(so.group(1))
        rir, rest = _RE_RIR.search(part), _RE_REST.search(part)
        keep_spec = timed or reps is None or "per" in spec
        note_parts = [p for p in ((spec if keep_spec else ""), note.rstrip(".")) if p]
        out.append({
            "code": _code_for_prose_name(name), "name": name, "sets": sets, "reps": reps,
            "rir": int(rir.group(1)) if rir else None,
            "rest_sec": int(rest.group(1)) if rest else None,
            "load": load.rstrip(".") or None, "note": " — ".join(note_parts) or None,
        })
    return out


def _parse_bullets(text: str) -> list[dict]:
    out: list[dict | None] = []
    skip = {"uppvärmning", "nedvarvning"}
    for line in text.splitlines():
        m = re.match(r"^- \*\*(.+?):\*\*\s*(.*)$", line)
        if m:
            name, spec = m.group(1), m.group(2)
            if name.casefold() in skip:
                out.append(None)
                continue
            first = spec.split(",", 1)[0].strip()
            timed = bool(_RE_TIMED.search(first))
            sr = _RE_SETS_REPS.search(first)
            sets = reps = None
            if sr and not timed:
                sets, reps = int(sr.group(1)), int(sr.group(2))
            elif sr:
                sets = int(sr.group(1))
            rir, rest = _RE_RIR.search(spec), _RE_REST.search(spec)
            note = first if timed else (f"{reps}{first[len(sr.group(0)):]}" if sr and first != sr.group(0) else None)
            out.append({
                "code": _code_for_prose_name(name), "name": name, "sets": sets, "reps": reps,
                "rir": int(rir.group(1)) if rir else None,
                "rest_sec": int(rest.group(1)) if rest else None,
                "load": "kroppsvikt", "note": note,
            })
            continue
        m = re.match(r"^\s+- _(.+)_\s*$", line)
        if m and out and out[-1] is not None:
            prev = out[-1]["note"]
            out[-1]["note"] = (prev + " — " if prev else "") + m.group(1)
    return [e for e in out if e is not None]


def exercises_from_logs(history: list[dict]) -> list[dict]:
    """Senast loggade styrkepasset som övningslista — sista reserven.

    När varken raden, förra planerade passet eller prosan ger en lista
    (alla pass framåt säger "samma som torsdag") är det adepten faktiskt
    gjorde sist den bästa förlagan. Progressionen räknar sedan vidare ur
    samma logg. Märks ``derived: "logged:<datum>"``.
    """
    performed = [
        h for h in history or []
        if h.get("exercise_name") and to_int(h.get("effort")) != -1
    ]
    if not performed:
        return []
    last_date = max(str(h.get("session_date") or "")[:10] for h in performed)
    out = []
    for h in performed:
        if str(h.get("session_date") or "")[:10] != last_date:
            continue
        out.append({
            "code": h.get("exercise_code") or None,
            "name": h["exercise_name"],
            "sets": to_int(h.get("sets")), "reps": to_int(h.get("reps")),
            "reps_min": None, "reps_max": None, "rir": None, "rest_sec": None,
            "load": None, "note": None, "derived": f"logged:{last_date}",
        })
    return out


def previous_strength_session(rows: list[dict], before_date: str, exercise_map=None) -> dict | None:
    """Senaste tidigare styrkepasset med en övningslista (max 21 dagar bakåt).

    För rader som bara säger "samma som torsdag". Returnerar raden med
    ``_exercises`` ifylld, eller None.
    """
    from datetime import date as _date, timedelta

    try:
        floor = (_date.fromisoformat(before_date[:10]) - timedelta(days=21)).isoformat()
    except ValueError:
        return None
    candidates = [
        r for r in rows
        if (r.get("sport") or "").strip().casefold() in ("styrka", "strength")
        and floor <= str(r.get("date") or "")[:10] < before_date[:10]
        and r.get("status") != "cancelled"
    ]
    for r in sorted(candidates, key=lambda r: str(r.get("date") or ""), reverse=True):
        exercises = planned_exercises(r, exercise_map)   # utan previous: inget arv i arv
        if exercises:
            return {**r, "_exercises": [
                {k: v for k, v in e.items() if k != "derived"} for e in exercises
            ]}
    return None


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
