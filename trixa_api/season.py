"""Säsongs-tidslinje för dashboarden — fas-staplar + tävlings-milstolpar.

Detta är ett **view-lager** (projektion), INTE den riktiga periodiseringsmotorn.
Den lägger ut faserna deterministiskt bakåt från tävlingsdatumet enligt samma
regler som engine använder (race-närhet) + fas-längderna i phases.yaml, och
komprimerar uthållighetsfaserna proportionellt om tiden inte räcker.

Ingen LLM, ingen DB. Ren funktion av (idag, race_date). Compliance-färgning
(plan vs actual per vecka) läggs på i ui.py eftersom den behöver DB-data.

Den dag en riktig periodiseringsmotor byggs (sparar fas-schemat i DB och
återanvänds av veckogeneratorn) ska den ersätta projektionen här.
"""

from __future__ import annotations

from datetime import date, timedelta

from coach.engine._loader import load_yaml


# Framåt-ordning fram till tävling (transition är efter loppet, ingår ej här).
_FORWARD_ORDER = ["prep", "base", "build", "peak", "race"]

# Fas-färger: sval → varm progression upp mot loppet. Presentationsdata.
_PHASE_COLORS = {
    "prep":  {"bg": "#ede9fe", "fg": "#5b21b6"},  # violett
    "base":  {"bg": "#dbeafe", "fg": "#1e40af"},  # blå
    "build": {"bg": "#fef3c7", "fg": "#92400e"},  # bärnsten
    "peak":  {"bg": "#fed7aa", "fg": "#9a3412"},  # orange
    "race":  {"bg": "#fee2e2", "fg": "#991b1b"},  # röd
}


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build_phase_timeline(today: date, race_date: date | None) -> dict | None:
    """Bygg en fas-tidslinje från denna vecka till tävlingsveckan.

    Returnerar None om tävlingsdatum saknas eller redan passerat.

    Struktur:
        {
          "total_weeks": int,        # antal veckor inkl. tävlingsveckan
          "ideal_weeks": int,        # summa av fasernas min-längder (för kontrast)
          "race_monday": date,
          "weeks": [ {index, monday, iso_year, iso_week, weeks_until_race,
                      phase, phase_label} ],
          "bars": [ {phase, label, weeks, bg, fg} ],   # sammanslagna fas-block
        }
    """
    if not race_date or race_date <= today:
        return None

    phases = load_yaml("phases.yaml")["phases"]
    start = _monday_of(today)
    race_monday = _monday_of(race_date)
    total_weeks = (race_monday - start).days // 7 + 1  # inkl. tävlingsveckan
    if total_weeks < 1:
        return None

    # Optimal fas per vecka = räkna bakåt från loppet, SAMMA mappning som
    # engine.phases._optimal_phase_for_race (race ≤2 v, peak 3-4, build 5-12,
    # base 13-24, prep >24). Tidslinjen visar alltså den OPTIMALA planen; vad
    # adepten faktiskt klarar (capad fas) syns som "Aktuell fas" i dashboarden.
    def _optimal(wur: int) -> str:
        if wur <= 2:
            return "race"
        if wur <= 4:
            return "peak"
        if wur <= 12:
            return "build"
        if wur <= 24:
            return "base"
        return "prep"

    phase_by_index = [_optimal((total_weeks - 1) - i) for i in range(total_weeks)]

    # Bygg vecko-lista
    weeks = []
    for i in range(total_weeks):
        wm = start + timedelta(weeks=i)
        iso = wm.isocalendar()
        ph = phase_by_index[i]
        weeks.append({
            "index": i,
            "monday": wm,
            "iso_year": iso[0],
            "iso_week": iso[1],
            "weeks_until_race": (total_weeks - 1) - i,
            "phase": ph,
            "phase_label": phases[ph]["name_sv"],
        })

    # Slå ihop sammanhängande faser till staplar
    bars: list[dict] = []
    for w in weeks:
        if bars and bars[-1]["phase"] == w["phase"]:
            bars[-1]["weeks"] += 1
        else:
            colors = _PHASE_COLORS.get(w["phase"], {"bg": "#e5e7eb", "fg": "#374151"})
            bars.append({
                "phase": w["phase"],
                "label": w["phase_label"],
                "weeks": 1,
                "bg": colors["bg"],
                "fg": colors["fg"],
            })

    ideal_weeks = sum(phases[p]["duration_weeks"][0] for p in _FORWARD_ORDER)

    return {
        "total_weeks": total_weeks,
        "ideal_weeks": ideal_weeks,
        "race_monday": race_monday,
        "weeks": weeks,
        "bars": bars,
    }


def race_label(race_date: date) -> str | None:
    """Snyggt tävlingsnamn från races.yaml om datumet matchar, annars None."""
    try:
        upcoming = load_yaml("races.yaml").get("upcoming") or []
    except Exception:  # noqa: BLE001
        return None
    iso = race_date.isoformat()
    for r in upcoming:
        if str(r.get("date"))[:10] == iso:
            return r.get("name")
    return None


def compliance_bucket(workouts: list[dict], today: date) -> str | None:
    """Följsamhet för en vecka baserat på pass-status (plan vs actual).

    done/deviated = du tränade (full kredit), missed = du tränade inte.
    Vilodagar, framtida pass och "idag" räknas inte. Returnerar None om
    veckan inte har några bedömbara (passerade) pass än.
    """
    did = 0
    total = 0
    for w in workouts:
        key = (w.get("status") or {}).get("key")
        if key in ("done", "deviated"):
            did += 1
            total += 1
        elif key == "missed":
            total += 1
    if total == 0:
        return None
    ratio = did / total
    if ratio >= 0.8:
        return "green"
    if ratio >= 0.5:
        return "yellow"
    return "red"
