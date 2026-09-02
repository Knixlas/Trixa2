"""Autoreglerad lastprogression för styrkepassen.

Passbanken beskrev modellen men ingen kod bar den. ``strength_MS.yaml``:

    "Adepten väljer vikt som matchar RIR:et, loggar den, och progredierar
    nästa gång samma RIR nås vid lägre ansträngning. Detta ÄR autoreglering."

Loggen tog emot vikt och ansträngning, men ingenting läste dem tillbaka.
Adepten fick minnas förra passets vikt själv och gissa nästa. Den här modulen
stänger loopen: senaste loggen för samma övning + protokollets repspann ger
nästa vikt och nästa reps, med en mening som säger varför.

Modellen är **dubbel progression**, standard för styrketräning och den enda
som får repspannen i ``strength.yaml`` att betyda något: kör inom spannet,
öka reps tills taket nås, öka då vikten och gå ned till golvet igen.
Ansträngningen (``exercise_logs.effort``) styr takten.

Ren funktion: inga DB-anrop, ingen LLM. Anroparen hämtar historiken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# exercise_logs.effort — skalan adepten bockar av på.
EFFORT_LIGHT = 1
EFFORT_MODERATE = 2
EFFORT_HARD = 3
EFFORT_TOO_HARD = 4
EFFORT_SKIPPED = -1

EFFORT_LABELS: dict[int, str] = {
    EFFORT_LIGHT: "lätt",
    EFFORT_MODERATE: "lagom",
    EFFORT_HARD: "tungt",
    EFFORT_TOO_HARD: "för tungt",
    EFFORT_SKIPPED: "hoppade över",
}

# Stegen adepten faktiskt kan lägga på stången. Ett förslag på 41,3 kg är
# oanvändbart i gymmet — avrunda till något som finns som vikter.
_INCREMENTS: tuple[tuple[float, float], ...] = (
    (10.0, 0.5),   # under 10 kg: småhantlar
    (40.0, 1.0),   # 10-40 kg: hantelrack
)
_DEFAULT_INCREMENT = 2.5  # skivstång

# Procentsatserna. Medvetet små: en missad ökning kostar en vecka, en för stor
# kostar ett pass eller en axel.
_BUMP_LIGHT = 0.05
_BUMP_MODERATE = 0.025
_BACKOFF_TOO_HARD = 0.05
_DELOAD = 0.10

# Tre pass på samma vikt utan att den lättat = fastnat, inte progression.
_STALL_SESSIONS = 3

_DEFAULT_REP_SPAN = 2  # reps_max när bara ett planerat rep-tal är känt


@dataclass(frozen=True)
class LoadSuggestion:
    """Vad adepten bör köra nästa gång, och varför."""

    weight: float | None          # kg; None = kroppsvikt
    reps: int | None
    sets: int | None
    reason: str                   # en mening, adept-vänd
    trend: str                    # new | up | hold | down | deload
    previous: dict[str, Any] | None = None  # loggraden förslaget vilar på
    warnings: list[str] = field(default_factory=list)


def round_load(kg: float) -> float:
    """Närmaste vikt som går att lasta på stången."""
    if kg <= 0:
        return 0.0
    step = _DEFAULT_INCREMENT
    for ceiling, increment in _INCREMENTS:
        if kg < ceiling:
            step = increment
            break
    return round(round(kg / step) * step, 2)


def _min_step(kg: float) -> float:
    """Minsta ökning som faktiskt går att lasta vid den här vikten."""
    for ceiling, increment in _INCREMENTS:
        if kg < ceiling:
            return increment
    return _DEFAULT_INCREMENT


def _fmt_kg(kg: float | None) -> str:
    if kg is None:
        return "kroppsvikt"
    text = f"{kg:.1f}".rstrip("0").rstrip(".")
    return f"{text.replace('.', ',')} kg"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _is_bodyweight(planned: dict) -> bool:
    """Kroppsviktsövning? Då progredierar reps, inte kilon."""
    load = str(planned.get("load") or planned.get("load_pct") or "").casefold()
    return "kroppsvikt" in load or "bodyweight" in load


def rep_span(planned: dict) -> tuple[int, int]:
    """Repspannet dubbel progression rör sig inom.

    Passet bär spannet när det kommer från en mall (``parameters.reps.range``);
    äldre rader har bara ett tal och får ett smalt spann runt det, så att
    logiken fungerar även på veckor som lagts innan spannet fanns.
    """
    low = _as_int(planned.get("reps_min"))
    high = _as_int(planned.get("reps_max"))
    planned_reps = _as_int(planned.get("reps"))
    if low and high and high >= low:
        return low, high
    if planned_reps:
        return planned_reps, planned_reps + _DEFAULT_REP_SPAN
    return 8, 8 + _DEFAULT_REP_SPAN


def history_key(name: Any, code: Any = None) -> str:
    """Nyckeln som binder ihop logg och plan för samma övning.

    Koden vinner när den finns — katalogens namn kan skrivas om utan att
    övningen blir en annan, och då ska historiken följa med.
    """
    code = str(code or "").strip() or None
    if code:
        return f"code:{code.casefold()}"
    return f"name:{str(name or '').strip().casefold()}"


def index_history(logs: Iterable[dict]) -> dict[str, list[dict]]:
    """Loggrader → historik per övning, nyast först.

    Varje rad indexeras på både kod och namn: en logg skriven innan koden
    fanns hittas fortfarande via namnet.
    """
    out: dict[str, list[dict]] = {}
    for log in logs or []:
        if not isinstance(log, dict):
            continue
        keys = {history_key(log.get("exercise_name"))}
        if log.get("exercise_code"):
            keys.add(history_key(None, log.get("exercise_code")))
        for key in keys:
            out.setdefault(key, []).append(log)
    for rows in out.values():
        rows.sort(key=lambda r: str(r.get("session_date") or ""), reverse=True)
    return out


def lookup_history(planned: dict, index: dict[str, list[dict]]) -> list[dict]:
    """Historiken för en planerad övning — koden först, namnet som reserv."""
    if planned.get("code"):
        rows = index.get(history_key(None, planned["code"]))
        if rows:
            return rows
    return index.get(history_key(planned.get("name"))) or []


def _performed(history: list[dict]) -> list[dict]:
    """Rader som faktiskt kördes. Ett överhoppat pass säger inget om lasten."""
    return [row for row in history if _as_int(row.get("effort")) != EFFORT_SKIPPED]


def _stalled(performed: list[dict], weight: float | None) -> bool:
    """Samma vikt tre pass i rad och den lättade aldrig."""
    if weight is None or len(performed) < _STALL_SESSIONS:
        return False
    for row in performed[:_STALL_SESSIONS]:
        row_weight = _as_float(row.get("weight_from"))
        if row_weight is None or abs(row_weight - weight) > 0.01:
            return False
        if (_as_int(row.get("effort")) or 0) < EFFORT_HARD:
            return False
    return True


def suggest_next(planned: dict, history: list[dict] | None = None) -> LoadSuggestion:
    """Nästa vikt och reps för en planerad övning, givet dess historik.

    ``planned`` är en post ur ``planned_sessions.exercises``; ``history`` är
    adeptens ``exercise_logs``-rader för samma övning, nyast först.
    """
    # Dödhäng "till nära utmattning", björnkryp "15 meter", planka "45 sek":
    # övningar utan rep-tal. Att hitta på ett (8) och sedan räkna progression
    # på det vore att föreslå åtta dödhäng. Vikten kan fortfarande följas
    # (farmer's walk); reps lämnas tomma.
    track_reps = planned.get("reps") is not None or bool(_as_int(planned.get("reps_min")))
    low, high = rep_span(planned) if track_reps else (0, 0)
    planned_sets = _as_int(planned.get("sets"))
    planned_reps = _as_int(planned.get("reps")) or (low or None)
    bodyweight = _is_bodyweight(planned)
    performed = _performed(history or [])

    if not performed:
        return _suggest_first_time(
            planned, planned_reps, planned_sets, bodyweight, bool(history)
        )

    last = performed[0]
    last_weight = _as_float(last.get("weight_from"))
    last_reps = _as_int(last.get("reps")) or planned_reps or 0
    last_sets = _as_int(last.get("sets")) or planned_sets
    effort = _as_int(last.get("effort")) or EFFORT_MODERATE
    when = str(last.get("session_date") or "")[:10]
    warnings: list[str] = []

    if not track_reps:
        return _suggest_untracked(
            last, last_weight, last_sets or planned_sets, effort, when, bodyweight
        )

    # Klarade inte repspannets golv? Då var lasten för tung oavsett vad rutan
    # sade — kroppen räknas före kryssrutan.
    if last_reps < low and effort < EFFORT_TOO_HARD:
        effort = EFFORT_TOO_HARD
        warnings.append(
            f"Loggade {last_reps} reps mot spannets {low} — behandlas som för tungt."
        )

    context = (
        f"Förra gången ({when}): {last_sets or planned_sets or '?'}×{last_reps} @ "
        f"{_fmt_kg(last_weight)}, kändes {EFFORT_LABELS.get(effort, 'oklart')}"
    )

    if bodyweight or last_weight is None:
        return _suggest_bodyweight(
            context, effort, last_reps, last_sets or planned_sets, low, high,
            last, warnings,
        )

    # Fastnat: samma vikt tre pass utan att den lättat. Att fortsätta trycka på
    # är inte tålamod, det är att stå still med skaderisk. Backa och bygg om.
    if _stalled(performed, last_weight):
        weight = round_load(last_weight * (1 - _DELOAD))
        return LoadSuggestion(
            weight=weight, reps=low, sets=last_sets or planned_sets,
            reason=(
                f"{context}. Tre pass på {_fmt_kg(last_weight)} utan att den lättat "
                f"— backa till {_fmt_kg(weight)} och bygg upp igen."
            ),
            trend="deload", previous=last, warnings=warnings,
        )

    if effort == EFFORT_LIGHT:
        if last_reps >= high:
            weight = _bump(last_weight, _BUMP_LIGHT)
            return LoadSuggestion(
                weight=weight, reps=low, sets=last_sets or planned_sets,
                reason=f"{context}. {_bump_phrase(weight, low, high)}",
                trend="up", previous=last, warnings=warnings,
            )
        reps = min(last_reps + 2, high)
        return LoadSuggestion(
            weight=last_weight, reps=reps, sets=last_sets or planned_sets,
            reason=f"{context}. Behåll {_fmt_kg(last_weight)} och ta {reps} reps.",
            trend="up", previous=last, warnings=warnings,
        )

    if effort == EFFORT_MODERATE:
        if last_reps >= high:
            weight = _bump(last_weight, _BUMP_MODERATE)
            return LoadSuggestion(
                weight=weight, reps=low, sets=last_sets or planned_sets,
                reason=f"{context}. {_bump_phrase(weight, low, high)}",
                trend="up", previous=last, warnings=warnings,
            )
        reps = min(last_reps + 1, high)
        return LoadSuggestion(
            weight=last_weight, reps=reps, sets=last_sets or planned_sets,
            reason=f"{context}. Samma vikt, ett rep till: {reps}.",
            trend="up", previous=last, warnings=warnings,
        )

    if effort == EFFORT_HARD:
        return LoadSuggestion(
            weight=last_weight, reps=last_reps, sets=last_sets or planned_sets,
            reason=(f"{context}. Kör samma igen — vikten sitter rätt tills den "
                    "känns lagom."),
            trend="hold", previous=last, warnings=warnings,
        )

    weight = round_load(last_weight * (1 - _BACKOFF_TOO_HARD))
    if weight >= last_weight:
        weight = round_load(last_weight - _min_step(last_weight))
    return LoadSuggestion(
        weight=max(weight, 0.0), reps=low, sets=last_sets or planned_sets,
        reason=(f"{context}. Ned till {_fmt_kg(weight)} och {low} reps — "
                "bygg upp igen därifrån."),
        trend="down", previous=last, warnings=warnings,
    )


def _bump(weight: float, pct: float) -> float:
    """Höj med procentsatsen, men aldrig mindre än ett faktiskt viktsteg."""
    return round_load(max(weight * (1 + pct), weight + _min_step(weight)))


def _bump_phrase(weight: float, low: int, high: int) -> str:
    """Motiveringen till en viktökning.

    "Taket i spannet nått" är begripligt när passet FÖRESKRIVER ett spann.
    Övningar utanför planen har inget — deras spann är låst vid det adepten
    körde — och då är formuleringen bara förvirrande: hen har aldrig sett
    något tak.
    """
    if low >= high:
        return f"Upp till {_fmt_kg(weight)}, samma {low} reps."
    return (f"Taket i spannet nått — upp till {_fmt_kg(weight)} "
            f"och tillbaka till {low} reps.")


def _suggest_untracked(
    last: dict, last_weight: float | None, sets: int | None,
    effort: int, when: str, bodyweight: bool,
) -> LoadSuggestion:
    """Övning utan rep-tal: tid, sträcka eller 'till utmattning'.

    Bär den vikt (farmer's walk) följer vikten ansträngningen som vanligt.
    Är den kroppsvikt finns inget tal att räkna på — säg det, i stället för
    att låtsas.
    """
    label = EFFORT_LABELS.get(effort, "oklart")
    sets_txt = f"{sets}×" if sets else ""
    if last_weight is None or bodyweight:
        context = f"Förra gången ({when}): {sets_txt}kroppsvikt, kändes {label}"
        hint = {
            EFFORT_LIGHT: "Öka tid eller sträcka.",
            EFFORT_MODERATE: "Håll eller öka något.",
            EFFORT_HARD: "Kör samma igen.",
            EFFORT_TOO_HARD: "Dra ned tid eller sträcka.",
        }.get(effort, "")
        return LoadSuggestion(
            weight=None, reps=None, sets=sets,
            reason=f"{context}. Inget rep-tal att räkna på — {hint}".rstrip(),
            trend="hold", previous=last,
        )
    context = f"Förra gången ({when}): {sets_txt}{_fmt_kg(last_weight)}, kändes {label}"
    if effort == EFFORT_LIGHT:
        weight = _bump(last_weight, _BUMP_LIGHT)
        return LoadSuggestion(weight=weight, reps=None, sets=sets, trend="up", previous=last,
                              reason=f"{context}. Upp till {_fmt_kg(weight)}.")
    if effort == EFFORT_MODERATE:
        weight = _bump(last_weight, _BUMP_MODERATE)
        return LoadSuggestion(weight=weight, reps=None, sets=sets, trend="up", previous=last,
                              reason=f"{context}. Upp till {_fmt_kg(weight)}.")
    if effort == EFFORT_HARD:
        return LoadSuggestion(weight=last_weight, reps=None, sets=sets, trend="hold",
                              previous=last, reason=f"{context}. Kör samma igen.")
    weight = round_load(last_weight * (1 - _BACKOFF_TOO_HARD))
    if weight >= last_weight:
        weight = round_load(last_weight - _min_step(last_weight))
    return LoadSuggestion(weight=max(weight, 0.0), reps=None, sets=sets, trend="down",
                          previous=last, reason=f"{context}. Ned till {_fmt_kg(weight)}.")


def _suggest_first_time(
    planned: dict, planned_reps: int, planned_sets: int | None,
    bodyweight: bool, had_skips: bool,
) -> LoadSuggestion:
    """Ingen utförd historik — be om ett startvärde i stället för att gissa."""
    rir = _as_int(planned.get("rir"))
    if bodyweight:
        reason = "Första gången — kör tekniskt rent och logga hur det kändes."
    elif rir is not None:
        reason = (
            f"Första gången — välj en vikt som lämnar {rir} reps i tanken, logga "
            "den, så räknar Trixa vidare härifrån."
        )
    else:
        reason = (
            "Första gången — välj vikt efter känsla och logga den, så räknar "
            "Trixa vidare härifrån."
        )
    if had_skips:
        reason = "Övningen är loggad som överhoppad tidigare. " + reason
    return LoadSuggestion(
        weight=_as_float(planned.get("weight_from")),
        reps=planned_reps,
        sets=planned_sets,
        reason=reason,
        trend="new",
        previous=None,
    )


def _suggest_bodyweight(
    context: str, effort: int, last_reps: int, sets: int | None,
    low: int, high: int, last: dict, warnings: list[str],
) -> LoadSuggestion:
    """Kroppsvikt har inga kilon att lägga på — progressionen sitter i reps."""
    if effort == EFFORT_LIGHT:
        if last_reps >= high:
            return LoadSuggestion(
                weight=None, reps=low, sets=sets,
                reason=(f"{context}. Taket nått på kroppsvikt — lägg på yttre vikt "
                        f"eller ta en tyngre variant, och gå tillbaka till {low} reps."),
                trend="up", previous=last, warnings=warnings,
            )
        reps = min(last_reps + 2, high)
        return LoadSuggestion(
            weight=None, reps=reps, sets=sets,
            reason=f"{context}. Ta {reps} reps.",
            trend="up", previous=last, warnings=warnings,
        )
    if effort == EFFORT_MODERATE:
        reps = min(last_reps + 1, high)
        return LoadSuggestion(
            weight=None, reps=reps, sets=sets,
            reason=f"{context}. Ett rep till: {reps}.",
            trend="up", previous=last, warnings=warnings,
        )
    if effort == EFFORT_HARD:
        return LoadSuggestion(
            weight=None, reps=last_reps, sets=sets,
            reason=f"{context}. Kör samma igen tills det känns lagom.",
            trend="hold", previous=last, warnings=warnings,
        )
    reps = max(last_reps - 2, min(low, last_reps), 1)
    return LoadSuggestion(
        weight=None, reps=reps, sets=sets,
        reason=f"{context}. Ned till {reps} reps och bygg upp igen.",
        trend="down", previous=last, warnings=warnings,
    )


def suggestions_by_name(logs: Iterable[dict] | None) -> dict[str, dict]:
    """Förslag per övningsnamn för övningar som INTE står i någon plan.

    Prosa-planerade pass och egna tillägg utanför planen har ingen post att
    hänga ett förslag på, men adepten har ändå en historik. Uppslaget görs på
    namnet hen skriver, och den planerade formen härleds ur den senaste
    loggraden — det är det enda som är känt om övningen.

    Utan protokoll finns inget repspann, och då kan dubbel progression inte
    köras: ett spann som följer med senast loggade reps flyttar taket varje
    gång, så reps klättrar i all evighet och vikten stiger aldrig. Här är
    reps därför låsta vid det adepten körde och progressionen sitter helt i
    vikten — samma enkla modell som avbockningen alltid beskrivit. Övningar
    utan loggad vikt är kroppsvikt och progredierar i reps som vanligt.
    """
    out: dict[str, dict] = {}
    for key, rows in index_history(logs or []).items():
        if not key.startswith("name:"):
            continue
        performed = _performed(rows)
        if not performed:
            continue
        last = performed[0]
        last_reps = _as_int(last.get("reps"))
        has_weight = _as_float(last.get("weight_from")) is not None
        stub = {
            "name": last.get("exercise_name"),
            "code": last.get("exercise_code"),
            "sets": _as_int(last.get("sets")),
            "reps": last_reps,
        }
        if last_reps and has_weight:
            # Låst spann → varje ansträngning utom "tungt" flyttar vikten.
            stub["reps_min"] = stub["reps_max"] = last_reps
        elif last_reps:
            stub["reps_min"], stub["reps_max"] = last_reps, last_reps + _DEFAULT_REP_SPAN
        suggestion = suggest_next(stub, rows)
        out[key[len("name:"):]] = {
            "name": last.get("exercise_name"),
            "code": last.get("exercise_code"),
            "sets": suggestion.sets,
            "reps": suggestion.reps,
            "weight": suggestion.weight,
            "reason": suggestion.reason,
            "trend": suggestion.trend,
        }
    return out


def apply_suggestions(
    exercises: list[dict],
    logs: Iterable[dict] | None,
    coach_prescribed: bool = False,
) -> list[dict]:
    """Berika en övningslista med nästa last, utan att röra originalet.

    Varje post får ``weight_from``/``reps`` satta till förslaget och en
    ``suggestion``-dict med motivering, trend och raden förslaget vilar på —
    så att både formuläret, passtexten och coachen ser samma sak.

    ``coach_prescribed``: passet är skrivet av en coach (eller adepten
    själv), inte genererat ur passbanken. Då är rep-talet en föreskrift, inte
    en startpunkt — "3×10, djupet ändras först när svullnaden varit tyst två
    veckor" får inte bli 3×12 för att förra passet kändes lätt. Reps står
    kvar som skrivet; vikten följer fortfarande ansträngningen, det är den
    coachen inte kan se från sitt håll.
    """
    index = index_history(logs or [])
    out: list[dict] = []
    for planned in exercises or []:
        if not isinstance(planned, dict):
            continue
        entry = dict(planned)
        suggestion = suggest_next(entry, lookup_history(entry, index))
        reps = suggestion.reps
        reason = suggestion.reason
        if coach_prescribed and planned.get("reps") is not None:
            reps = _as_int(planned.get("reps"))
            if suggestion.reps is not None and suggestion.reps != reps:
                reason = f"{reason} Reps enligt coachens pass: {reps}."
        if suggestion.weight is not None:
            entry["weight_from"] = suggestion.weight
        if reps is not None:
            entry["reps"] = reps
        entry["suggestion"] = {
            "weight": suggestion.weight,
            "reps": reps,
            "sets": suggestion.sets,
            "reason": reason,
            "trend": suggestion.trend,
            "previous": suggestion.previous,
            "warnings": suggestion.warnings,
        }
        out.append(entry)
    return out
