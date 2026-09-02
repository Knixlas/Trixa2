"""Logik för träningspass och volymfördelning.

Källor: data/workouts.yaml + data/phase_details.yaml.

Funktioner:
- select_workout_types(): filtrera passtyper baserat på fas, period, vecka
- distribute_weekly_hours(): fördela total tid mellan sim/cykel/löpning
- max_session_minutes(): max passlängd för en disciplin i en fas
- hard_training_cap_minutes(): tak för tid i Zon 3+ för veckan
"""

from __future__ import annotations

from typing import Literal

from ._loader import load_yaml


PhaseCode = Literal["prep", "base", "build", "peak", "race", "transition"]
Discipline = Literal["swim", "bike", "run"]


# ---------- Passtypsval ----------


def select_workout_types(
    phase: PhaseCode,
    period: str | None,
    week_in_period: int,
    weeks_in_period: int,
) -> list[str]:
    """Returnera tillåtna passtypskoder för en specifik vecka.

    Hanterar reglerna från 2.2.x:
    - "ej sista veckan" → uteslut den sista veckan i perioden
    - "endast sista veckan" → ta bara med under sista veckan
    - "endast i period X" → ta bara med om period matchar

    Args:
        phase: fas-kod (prep/base/build/...)
        period: t.ex. "base_2", eller None om fasen saknar perioder
        week_in_period: 1-indexerad
        weeks_in_period: totalt antal veckor i perioden

    Returns:
        Lista med passtypskoder, t.ex. ["AE", "SS", "ME", "BW"]
    """
    return [
        d["code"] for d in workout_type_decisions(phase, period, week_in_period, weeks_in_period)
        if d["allowed"]
    ]


def workout_type_decisions(
    phase: PhaseCode,
    period: str | None,
    week_in_period: int,
    weeks_in_period: int,
) -> list[dict]:
    """Varje passtyp i fasen med beslut OCH skäl.

    ``select_workout_types`` gav bara koderna. När TE/ME försvann sista
    veckan i cykeln kunde ingen yta säga "vilovecka — hårda kategorier
    borttagna"; coachen såg en tunnare vecka utan något att peka på
    (docs/12 I6). Engine-output ska bära reason.
    """
    details = load_yaml("phase_details.yaml")["phase_details"]
    if phase not in details:
        raise ValueError(f"Okänd fas: {phase}")

    workout_types = details[phase].get("workout_types", [])
    is_last_week = week_in_period == weeks_in_period

    out: list[dict] = []
    for wt in workout_types:
        constraint = wt.get("constraint", "always")
        code = wt["code"]

        if constraint == "always":
            allowed, why = True, "alltid i fasen"
        elif constraint == "not_last_week":
            allowed = not is_last_week
            why = "uteslutet: sista veckan i cykeln (vilovecka)" if not allowed else "ej sista veckan"
        elif constraint == "last_week_only":
            allowed = is_last_week
            why = "bara sista veckan i cykeln" if allowed else "uteslutet: bara sista veckan"
        elif constraint == "period_only":
            allowed = period in wt.get("periods", [])
            why = (f"period {period}" if allowed
                   else f"uteslutet: bara i {', '.join(wt.get('periods', []))}")
        else:
            raise ValueError(f"Okänd constraint: {constraint}")

        # Extra flagga: uteslut sista veckan (återhämtningsveckan) även för
        # period_only-kategorier — kombinerar period-villkor med vilovecka.
        if allowed and wt.get("exclude_last_week") and is_last_week:
            allowed, why = False, "uteslutet: vilovecka (exclude_last_week)"

        out.append({"code": code, "allowed": allowed, "reason": why})
    return out


# ---------- Volymfördelning ----------


def distribute_weekly_hours(
    phase: PhaseCode,
    total_hours: float,
) -> dict[str, float]:
    """Fördela totala veckotimmar mellan discipliner enligt fasens split.

    Standard är 35% löp / 45% cykel / 20% sim (gäller prep, base, build, peak).
    För race och transition är fördelningen flexibel — returnerar standardsplit
    som fallback men kallaren bör veta att den är vägledande.

    Returns:
        Dict med timmar per disciplin, t.ex. {"run": 3.5, "bike": 4.5, "swim": 2.0}
    """
    details = load_yaml("phase_details.yaml")["phase_details"]
    split = details[phase].get("discipline_split")

    if split is None:
        # Fallback för race/transition
        split = load_yaml("phase_details.yaml")["default_discipline_split"]

    return {disc: total_hours * pct for disc, pct in split.items()}


# ---------- Passlängd ----------


def max_session_minutes(
    phase: PhaseCode,
    discipline: Discipline,
    period: str | None = None,
) -> int | None:
    """Returnera max passlängd i minuter för en disciplin i en fas.

    Värdet i phase_details får vara ett heltal ELLER en dict per period
    (base: långpasset växer base_1 → base_3). Vid dict utan angiven period
    returneras periodernas max (konservativt tak).

    Returnerar None om fasen inte specificerar (t.ex. race, transition).
    Endast run och bike har max-värden definierade i källdokumenten.
    """
    details = load_yaml("phase_details.yaml")["phase_details"]
    max_session = details[phase].get("max_session_minutes")
    if max_session is None:
        return None
    value = max_session.get(discipline)
    if isinstance(value, dict):
        if period is not None and period in value:
            return value[period]
        return max(value.values())
    return value


# ---------- Hård träning ----------


def hard_training_cap_minutes(
    phase: PhaseCode,
    total_weekly_minutes: float,
    previous_week_hard_minutes: float | None = None,
) -> dict[str, float | str | None]:
    """Beräkna tak för hård träning (Zon 3+) den här veckan.

    Reglerna varierar per fas:
    - prep: max 10% av total träningstid, 50/50 mellan cykel och löpning
    - base/build: öka max 10% jmf föregående vecka (delta-cap)
    - peak: reducera
    - race: minimal
    - transition: ingen

    Returns:
        Dict med:
            mode: regeltyp
            cap_minutes: rekommenderat max i minuter (None om regeln är "ingen")
            bike_minutes: rekommenderat för cykel (om 50/50-regel)
            run_minutes: rekommenderat för löpning (om 50/50-regel)
            note: förklarande text
    """
    details = load_yaml("phase_details.yaml")["phase_details"]
    rule = details[phase].get("hard_training_rule", {})
    mode = rule.get("mode")

    if mode == "absolute_share":
        share = rule.get("max_share_of_total", 0.10)
        cap = total_weekly_minutes * share
        split = rule.get("bike_run_split", [0.5, 0.5])
        return {
            "mode": mode,
            "cap_minutes": cap,
            "bike_minutes": cap * split[0],
            "run_minutes": cap * split[1],
            "note": f"Max {share * 100:.0f}% av totaltid, fördelat {split[0]:.0%}/{split[1]:.0%}",
        }

    if mode == "weekly_delta_cap":
        max_increase = rule.get("max_increase_vs_prev_week", 0.10)
        if previous_week_hard_minutes is None:
            return {
                "mode": mode,
                "cap_minutes": None,
                "bike_minutes": None,
                "run_minutes": None,
                "note": "Föregående veckas hårda träning saknas — kan ej beräkna delta-tak",
            }
        cap = previous_week_hard_minutes * (1 + max_increase)
        return {
            "mode": mode,
            "cap_minutes": cap,
            "bike_minutes": None,
            "run_minutes": None,
            "note": f"Max +{max_increase * 100:.0f}% mot förra veckans hårda träning",
        }

    if mode == "reduce":
        return {
            "mode": mode,
            "cap_minutes": None,
            "bike_minutes": None,
            "run_minutes": None,
            "note": rule.get("note", "Minska hård träning"),
        }

    if mode == "minimal":
        return {
            "mode": mode,
            "cap_minutes": None,
            "bike_minutes": None,
            "run_minutes": None,
            "note": rule.get("note", "Mycket lite hård träning"),
        }

    # mode == "none" eller saknas helt
    return {
        "mode": "none",
        "cap_minutes": None,
        "bike_minutes": None,
        "run_minutes": None,
        "note": "Ingen hård träning denna fas",
    }
