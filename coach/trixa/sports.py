"""Ett register för sportvokabulären. Alla lager läser härifrån.

Kodöversynen 2026-09-02 (docs/12, avsnitt E) räknade tretton oberoende
översättningstabeller mellan svenska etiketter, engelska nycklar, TP:s
sporttyper och Stravas aktivitetstyper — och de motsade redan varandra:
Yoga var vila i ett lager och egen gren i två andra; brick kunde aldrig
bli "Genomförd" för att utfört-sidan inte kände igen ordet; MCP sparade
"Cykling" verbatim, som dashboarden inte kunde matcha och TP-skrivaren
skickade som "Other".

Här finns varje gren en gång, med alla sina stavningar. Lagren får
derivera det de behöver (``canon``, ``sv``, ``tp_name``, ``from_tp_id``)
i stället för att bära egna kopior.

Nycklarna är engelska/snake_case (kodkonvention); etiketterna svenska
(innehållskonvention). ``planned_sessions.sport`` lagras som ``sv``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sport:
    key: str                       # intern disciplin: swim/bike/run/…
    sv: str                        # lagringsnamn i planned_sessions + etikett
    aliases: frozenset[str]        # alla stavningar, gemener
    tp_name: str | None = None     # TP:s sportnamn (structure.SPORT_TYPE_MAP)
    tp_type_ids: tuple[int, ...] = ()   # TP workoutTypeValueId
    strava_types: tuple[str, ...] = ()
    is_training: bool = True       # räknas i volym/följsamhet
    is_rest: bool = False          # planerad frånvaro av träning
    substrings: tuple[str, ...] = field(default_factory=tuple)  # fri text → gren


SPORTS: dict[str, Sport] = {
    s.key: s for s in (
        Sport("swim", "Sim", frozenset({"sim", "simning", "swim", "swimming", "openwaterswim"}),
              tp_name="Swim", tp_type_ids=(1,), strava_types=("Swim", "OpenWaterSwim"),
              substrings=("swim", "sim")),
        Sport("bike", "Cykel", frozenset({"cykel", "cykling", "bike", "biking", "cycling",
                                         "ride", "mtb", "virtualride", "ebikeride",
                                         "mountainbikeride", "gravelride"}),
              tp_name="Bike", tp_type_ids=(2, 8),
              strava_types=("Ride", "VirtualRide", "EBikeRide", "MountainBikeRide", "GravelRide"),
              substrings=("cycl", "bike", "ride", "cykl")),
        Sport("run", "Löpning", frozenset({"löpning", "lopning", "löp", "run", "running",
                                          "trailrun", "virtualrun"}),
              tp_name="Run", tp_type_ids=(3,), strava_types=("Run", "TrailRun", "VirtualRun"),
              substrings=("run", "löp", "lop")),
        Sport("strength", "Styrka", frozenset({"styrka", "strength", "weighttraining",
                                              "workout", "gym"}),
              tp_name="Strength", tp_type_ids=(9,), strava_types=("WeightTraining", "Workout"),
              substrings=("strength", "weight", "styrk")),
        Sport("brick", "Brick", frozenset({"brick"}), tp_name="Brick", tp_type_ids=(4,)),
        Sport("yoga", "Yoga", frozenset({"yoga"}), tp_name="Other", strava_types=("Yoga",),
              substrings=("yoga",)),
        # Promenad/vandring bär ingen träningsavsikt: räknas inte i volym och
        # bryter ingen vilodag — men det är en aktivitet, inte frånvaro av en.
        Sport("walk", "Promenad", frozenset({"promenad", "vandring", "walk", "hike",
                                            "walking", "hiking"}),
              tp_name="Walk", tp_type_ids=(13,), strava_types=("Walk", "Hike"),
              is_training=False),
        Sport("rest", "Vila", frozenset({"vila", "rest", "recovery", "dayoff", "vilodag"}),
              tp_name="DayOff", tp_type_ids=(7,), is_training=False, is_rest=True),
        Sport("other", "Övrigt", frozenset({"other", "övrigt", "crosstrain", "rodd", "rowing",
                                           "xcski", "custom", "race"}),
              tp_name="Other", tp_type_ids=(5, 6, 10, 11, 12), is_training=False),
    )
}

_BY_ALIAS: dict[str, str] = {
    alias: s.key for s in SPORTS.values() for alias in s.aliases
}
_BY_TP_ID: dict[int, str] = {
    tid: s.key for s in SPORTS.values() for tid in s.tp_type_ids
}
_BY_STRAVA: dict[str, str] = {
    t: s.key for s in SPORTS.values() for t in s.strava_types
}


def canon(value: str | None, default: str | None = None) -> str | None:
    """Vilken stavning som helst → disciplin-nyckel.

    Exakt alias först, sedan delsträng ("GravelRide", "Löp 30 min"). Okänt →
    ``default`` (None), aldrig råsträngen: en okänd sport ska hanteras som
    okänd, inte smyga vidare som en egen gren ingen annan känner till.
    """
    raw = (value or "").strip()
    if not raw:
        return default
    low = raw.lower()
    if low in _BY_ALIAS:
        return _BY_ALIAS[low]
    for s in SPORTS.values():
        if any(sub in low for sub in s.substrings):
            return s.key
    return default


def sv(key: str | None) -> str:
    """Disciplin → lagringsnamn/etikett på svenska."""
    if key in SPORTS:
        return SPORTS[key].sv
    return SPORTS["other"].sv


def normalize_sv(value: str | None) -> str:
    """Vilken stavning som helst → kanoniskt svenskt lagringsnamn.

    "Cykling", "biking", "bike" → "Cykel". Det är den här funktionen varje
    skrivare mot planned_sessions.sport ska gå genom.
    """
    key = canon(value)
    return sv(key) if key else sv("other")


def tp_name(key: str | None) -> str:
    return (SPORTS.get(key or "") or SPORTS["other"]).tp_name or "Other"


def from_tp_id(type_id: int | None) -> str:
    return _BY_TP_ID.get(int(type_id or 0), "other")


def from_strava(activity_type: str | None) -> str:
    return _BY_STRAVA.get(activity_type or "", canon(activity_type, "other") or "other")


def is_training(key: str | None) -> bool:
    return bool(key in SPORTS and SPORTS[key].is_training)


def is_rest(key: str | None) -> bool:
    return bool(key in SPORTS and SPORTS[key].is_rest)


TRAINING_KEYS: frozenset[str] = frozenset(k for k, s in SPORTS.items() if s.is_training)
PLANNABLE_KEYS: tuple[str, ...] = ("swim", "bike", "run", "strength", "brick", "yoga", "walk", "rest")


def status_kind(key: str | None) -> str:
    """Hur plan-mot-utfall bedöms för grenen: 'rest' (vila hållen/bruten)
    eller 'training' (genomförd/avviken/missad). Promenad hör till vila-
    sidan: en planerad promenad som inte blev av är inget missat pass."""
    return "training" if is_training(key) else "rest"
LOGGABLE_KEYS: tuple[str, ...] = ("swim", "bike", "run", "strength", "yoga")
