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


# ---------- Hela säsongen: optimal plan (fas + volym) vs faktiskt utfall ----------

# Hur många veckor historik vyn visar bakåt (för läsbarhet — inte hela ideal-
# perioden om den är lång). Framåt visas alltid hela vägen till loppet.
SEASON_LOOKBACK_WEEKS = 12

# Optimal veckovolym som ANDEL av peak-målet (peak = build-fasens volym).
# Deterministisk kurva: ramp upp till peak (slutet av build), tapering ner mot
# loppet. Faserna färgar banden; den här kurvan ger volym-linjen.
_VOL_START_FRAC = 0.45   # tidig prep ≈ 45% av peak
_VOL_TAPER_FRAC = 0.40   # tävlingsveckan ≈ 40% (tapering)


def _optimal_phase_for(weeks_until_race: int) -> str:
    """Samma mappning som engine.phases (race ≤2, peak 3-4, build 5-12,
    base 13-24, prep >24)."""
    if weeks_until_race <= 2:
        return "race"
    if weeks_until_race <= 4:
        return "peak"
    if weeks_until_race <= 12:
        return "build"
    if weeks_until_race <= 24:
        return "base"
    return "prep"


def _optimal_volume(week_idx: int, race_idx: int, peak_idx: int, peak_hours: float) -> float:
    """Optimal veckovolym (h) för en vecka: ramp till peak_idx, sen tapering.

    peak_idx = veckan där volymen toppar (sista byggveckan före toppning).
    """
    if peak_hours <= 0:
        return 0.0
    if week_idx <= peak_idx:
        # Ramp _VOL_START_FRAC → 1.0
        frac = 1.0 if peak_idx <= 0 else (
            _VOL_START_FRAC + (1.0 - _VOL_START_FRAC) * (week_idx / peak_idx)
        )
    else:
        # Tapering 1.0 → _VOL_TAPER_FRAC fram till loppet
        span = max(race_idx - peak_idx, 1)
        frac = 1.0 - (1.0 - _VOL_TAPER_FRAC) * ((week_idx - peak_idx) / span)
    return round(peak_hours * frac, 1)


def build_season_plan(
    today: date,
    race_date: date | None,
    peak_hours: float,
    actual_by_week: dict[tuple[int, int], float] | None = None,
    compliance_by_week: dict[tuple[int, int], str] | None = None,
    lookback_weeks: int = SEASON_LOOKBACK_WEEKS,
) -> dict | None:
    """Hela träningsperioden: optimal fas + optimal volym per vecka, mot utfall.

    Spänner ``lookback_weeks`` bakåt (historik) → tävlingsveckan (framåt). Varje
    vecka får optimal fas, optimal volym (h) och — för passerade veckor —
    faktisk volym + följsamhet. Ren funktion; DB-aggregaten skickas in.

    Returnerar None om tävling saknas/passerat.
    """
    if not race_date or race_date <= today:
        return None
    actual_by_week = actual_by_week or {}
    compliance_by_week = compliance_by_week or {}

    phases = load_yaml("phases.yaml")["phases"]
    this_monday = _monday_of(today)
    race_monday = _monday_of(race_date)
    weeks_to_race = (race_monday - this_monday).days // 7

    ideal_weeks = sum(phases[p]["duration_weeks"][0] for p in _FORWARD_ORDER)

    # Starta historik vid lookback, men inte före den ideala periodens start.
    season_start = this_monday - timedelta(weeks=lookback_weeks)
    ideal_start = race_monday - timedelta(weeks=ideal_weeks - 1)
    if season_start < ideal_start:
        season_start = ideal_start

    total_weeks = (race_monday - season_start).days // 7 + 1
    race_idx = total_weeks - 1
    # Volymtoppen: sista byggveckan (weeks_until_race == 5), annars strax före loppet.
    peak_idx = next(
        (i for i in range(total_weeks) if (race_idx - i) == 5),
        max(0, race_idx - 3),
    )

    weeks: list[dict] = []
    max_hours = peak_hours
    for i in range(total_weeks):
        wm = season_start + timedelta(weeks=i)
        iso = wm.isocalendar()
        wur = race_idx - i
        ph = _optimal_phase_for(wur)
        opt_h = _optimal_volume(i, race_idx, peak_idx, peak_hours)
        key = (iso[0], iso[1])
        is_past = wm < this_monday
        is_current = wm == this_monday
        act_h = actual_by_week.get(key)
        max_hours = max(max_hours, opt_h, act_h or 0)
        weeks.append({
            "index": i, "monday": wm, "monday_iso": wm.isoformat(),
            "iso_year": iso[0], "iso_week": iso[1], "weeks_until_race": wur,
            "phase": ph, "phase_label": phases[ph]["name_sv"],
            "optimal_hours": opt_h,
            "actual_hours": round(act_h, 1) if act_h is not None else None,
            "compliance": compliance_by_week.get(key),
            "is_past": is_past, "is_current": is_current, "future": wm > this_monday,
        })

    # Slå ihop sammanhängande faser → staplar (för fas-bandet ovanför)
    bars: list[dict] = []
    for w in weeks:
        if bars and bars[-1]["phase"] == w["phase"]:
            bars[-1]["weeks"] += 1
        else:
            c = _PHASE_COLORS.get(w["phase"], {"bg": "#e5e7eb", "fg": "#374151"})
            bars.append({"phase": w["phase"], "label": w["phase_label"],
                         "weeks": 1, "bg": c["bg"], "fg": c["fg"]})

    # Avstämning för innevarande vecka
    cur = next((w for w in weeks if w["is_current"]), None)
    now = None
    if cur:
        opt = cur["optimal_hours"]
        act = cur["actual_hours"] or 0.0
        now = {
            "optimal_hours": opt,
            "actual_hours": act,
            "gap_pct": round((act / opt - 1) * 100) if opt > 0 else None,
            "phase_label": cur["phase_label"],
        }

    return {
        "race_monday": race_monday, "total_weeks": total_weeks,
        "weeks_to_race": weeks_to_race, "ideal_weeks": ideal_weeks,
        "compressed": ideal_weeks > (weeks_to_race + 1),
        "peak_hours": round(peak_hours, 1),
        "max_hours": round(max_hours, 1) or 1.0,
        "weeks": weeks, "bars": bars, "now": now,
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
