"""Rendera ett pass till människoläsbar markdown.

Multi-disciplin: hanterar swim/bike/run med rätt zon-presentation
och segment-formatering per disciplin.

Använd `render_workout(workout, profile, drill_map=...)` som huvudfunktion.
Saknade testvärden i profilen → segmenten renderas med "Z2" som etikett
utan konkreta värden.
"""

from __future__ import annotations

from typing import Any

from .loader import AthleteProfile
from .zones import compute_zones, ZoneRange, ZoneSet


# ---------- Formatering av målvärden ----------


def _fmt_pace_per_100m(sec: float) -> str:
    """Sekunder per 100m → 'M:SS.S'-format."""
    m, s = divmod(round(sec, 1), 60)
    return f"{int(m)}:{s:04.1f}"


def _fmt_pace_per_km(sec: float) -> str:
    """Sekunder per km → 'M:SS'-format (utan decimaler)."""
    sec_int = round(sec)
    m, s = divmod(sec_int, 60)
    return f"{int(m)}:{int(s):02d}"


def _fmt_pace_range_swim(lo: float, hi: float) -> str:
    return f"{_fmt_pace_per_100m(lo)}–{_fmt_pace_per_100m(hi)}/100m"


def _fmt_pace_range_run(lo: float, hi: float) -> str:
    # Sim: lo < hi (sekundtal). Run: snabbare pace = lägre sekundtal,
    # presenteras som "snabbast–långsammast"
    return f"{_fmt_pace_per_km(lo)}–{_fmt_pace_per_km(hi)}/km"


def _fmt_zone_target(zone: int, zr: ZoneRange | None, discipline: str) -> str:
    """Formatera en zon med sina målvärden för en given disciplin.

    Sim: "Z2 (1:38.0–1:40.0/100m)"
    Bike: "Z2 (110–145 W, 132–142 bpm)"
    Run: "Z2 (5:30–6:00/km, 138–148 bpm)"

    Om värden saknas (ingen profildata): "Z2"
    """
    label = f"Z{zone}"
    if zr is None:
        return label

    parts: list[str] = []

    if discipline == "swim" and zr.pace_sec_per_100m:
        lo, hi = zr.pace_sec_per_100m
        parts.append(_fmt_pace_range_swim(lo, hi))

    if discipline == "bike":
        if zr.watts:
            parts.append(f"{zr.watts[0]}–{zr.watts[1]} W")
        if zr.hr_bpm:
            parts.append(f"{zr.hr_bpm[0]}–{zr.hr_bpm[1]} bpm")

    if discipline == "run":
        if zr.pace_sec_per_km:
            lo, hi = zr.pace_sec_per_km
            parts.append(_fmt_pace_range_run(lo, hi))
        if zr.hr_bpm:
            parts.append(f"{zr.hr_bpm[0]}–{zr.hr_bpm[1]} bpm")

    if not parts:
        return label
    return f"{label} ({', '.join(parts)})"


# ---------- Segment-rendering per disciplin ----------


def _segment_quantity(seg: dict, discipline: str) -> str:
    """Returnerar 'kvantitetsdelen' av segmentet: '4×400m' eller '4×4 min'."""
    sets = seg.get("sets", 1)
    dist = seg.get("distance_m")
    dur = seg.get("duration_min")

    # Swim är distance-baserat, bike/run är duration-baserat — men båda
    # förekommer beroende på pass. Vi väljer baserat på vad som finns.
    if dist is not None:
        unit = "m"
        amount = dist
    elif dur is not None:
        # Visa i min, även om det är decimaltal
        unit = "min"
        amount = dur
    else:
        return ""

    if sets > 1:
        return f"{sets}×{amount}{unit}" if unit == "m" else f"{sets}×{amount} {unit}"
    return f"{amount}{unit}" if unit == "m" else f"{amount} {unit}"


def _segment_label(seg_type: str, sets: int) -> str:
    """Rubrik för segment-typen."""
    labels = {
        "warmup": "Uppvärmning",
        "cooldown": "Nedvarvning",
        "drills": "Drills",
        "kick": "Kick",
        "pull": "Pull",
        "build": "Build",
        "sprint": "Sprint",
        "continuous": "Kontinuerligt",
        "test": "TEST",
        "rest": "Vila",
        "recovery": "Återhämtning",
    }
    if seg_type == "main":
        return "Set" if sets == 1 else "Huvudset"
    return labels.get(seg_type, seg_type.capitalize())


def _render_drills_segment(seg: dict, drill_map: dict | None) -> str:
    drills = seg.get("drills", [])
    if drill_map and drills:
        names = [drill_map.get(d, {}).get("name", d) for d in drills]
        return f" ({', '.join(names)})"
    if drills:
        return f" ({', '.join(drills)})"
    return ""


def _render_segment(
    seg: dict,
    zoneset: ZoneSet,
    drill_map: dict | None = None,
) -> str:
    """Rendera ett segment till en markdown-bullet."""
    seg_type = seg.get("segment", "?")
    sets = seg.get("sets", 1)
    rest = seg.get("rest_sec")
    desc = seg.get("description", "")
    equip = seg.get("equipment", [])

    label = _segment_label(seg_type, sets)
    quantity = _segment_quantity(seg, zoneset.discipline)

    # Specialfall: 'rest' har inget annat innehåll än vilotid
    if seg_type == "rest":
        line = f"**{label}:** {rest}s"
        if desc:
            line += f" — {desc}"
        return f"- {line}"

    # Specialfall: 'continuous' med duration_pct (oresolverat template)
    if seg_type == "continuous" and seg.get("duration_pct") and not seg.get("duration_min"):
        pct = seg["duration_pct"]
        quantity = f"{int(pct * 100)}% av totaltiden"

    line = f"**{label}:**"
    if quantity:
        line += f" {quantity}"
    if rest is not None and rest > 0 and seg_type != "rest":
        line += f", {rest}s vila"

    # Drills: lägg in drill-namn efter quantity
    if seg_type == "drills":
        line += _render_drills_segment(seg, drill_map)

    # Zon-mål eller effort-descriptor
    zone = seg.get("zone")
    zones_per_set = seg.get("zones_per_set")
    effort = seg.get("effort_descriptor")

    if effort:
        line += f" — {effort}"
    elif zone is not None:
        zr = zoneset.get(zone)
        line += f" @ {_fmt_zone_target(zone, zr, zoneset.discipline)}"
    elif zones_per_set:
        zone_strs = [f"Z{z}" for z in zones_per_set]
        line += f" @ {'/'.join(zone_strs)}"

    # Bike-specifika fält
    cadence = seg.get("cadence_rpm")
    if cadence:
        if isinstance(cadence, list) and len(cadence) == 2:
            line += f" | kadens {cadence[0]}–{cadence[1]} rpm"
        else:
            line += f" | kadens {cadence} rpm"

    if seg.get("erg_mode") is True:
        line += " | ERG"
    elif seg.get("erg_mode") is False and zoneset.discipline == "bike":
        # Bara visa när det är explicit override till False
        line += " | fri-ride"

    # Utrustning
    if equip:
        line += f" [{', '.join(equip)}]"

    out = [f"- {line}"]
    if desc:
        out.append(f"   - _{desc}_")
    return "\n".join(out)


# ---------- Pass-rendering ----------


def _render_summary(workout: dict) -> str:
    """En rad med totaldistans/duration och spann."""
    if workout.get("parameterized"):
        params = workout.get("parameters", {})
        dur = params.get("duration_min", {})
        if isinstance(dur, dict):
            lo = dur.get("range", [dur.get("default", "?")])[0] if isinstance(dur.get("range"), list) else dur.get("default", "?")
            hi = dur.get("range", [dur.get("default", "?")])[-1] if isinstance(dur.get("range"), list) else dur.get("default", "?")
            default = dur.get("default", "?")
            return f"**Parameteriserat pass** — duration {lo}–{hi} min (default {default})"
        return "**Parameteriserat pass**"

    td = workout.get("total_distance_m")
    dur = workout.get("total_duration_min", {})
    est = dur.get("estimated", "?")
    rng = dur.get("flexible_range", [est, est])

    if td:
        return f"**Total:** ~{td}m, ~{est} min (spann {rng[0]}–{rng[1]} min)"
    return f"**Total:** ~{est} min (spann {rng[0]}–{rng[1]} min)"


def _render_drill_quick_ref(
    workout: dict, drill_map: dict[str, dict] | None
) -> list[str]:
    """Lista referenserade drills med kort syfte."""
    if not drill_map:
        return []
    referenced: list[str] = []
    for seg in workout.get("main_set", []):
        if seg.get("segment") != "drills":
            continue
        for d in seg.get("drills", []):
            if d in drill_map and d not in referenced:
                referenced.append(d)
    if not referenced:
        return []

    out = ["**Drill-snabbreferens:**"]
    for d in referenced:
        drill = drill_map[d]
        intent = drill.get("intent", "").strip().split("\n")[0]
        out.append(f"- **{drill['name']}** — {intent}")
    return out


def _render_bike_setting(workout: dict) -> str | None:
    """Bike: visa setting/trainer/outdoor-info."""
    if workout.get("discipline") != "bike":
        return None
    parts: list[str] = []
    setting = workout.get("setting")
    if setting:
        parts.append(f"Setting: {setting}")
    if workout.get("requires_trainer"):
        parts.append("kräver trainer")
    if workout.get("outdoor_only"):
        parts.append("endast utomhus")
    if workout.get("erg_mode") is True:
        parts.append("ERG default")
    if not parts:
        return None
    return f"*{', '.join(parts)}*"


def render_workout(
    workout: dict[str, Any],
    profile: AthleteProfile | None = None,
    drill_map: dict[str, dict] | None = None,
) -> str:
    """Rendera ett pass till markdown.

    Args:
        workout: pass-dict (parameterized templates bör resolvas innan)
        profile: adept-profil för zonberäkningar; om None används saknade zoner
        drill_map: {code: drill_dict} för drill-namn-uppslagning

    Returns:
        Markdown-sträng redo för visning.
    """
    discipline = workout.get("discipline", "swim")
    profile = profile or AthleteProfile()
    zoneset = compute_zones(discipline, profile)

    out: list[str] = []

    # Rubrik
    out.append(f"## {workout['code']} — {workout['name']}")
    out.append(
        f"*Kategori: {workout['category']} ({workout['type_code']}) | "
        f"Disciplin: {discipline} | "
        f"Faser: {', '.join(workout['phase_appropriate'])}*"
    )

    # Bike-specifik info-rad
    bike_info = _render_bike_setting(workout)
    if bike_info:
        out.append(bike_info)

    out.append("")
    out.append("**Syfte:**")
    out.append(workout["intent"].strip())
    out.append("")
    out.append(_render_summary(workout))
    out.append("")
    out.append("**Upplägg:**")
    for seg in workout["main_set"]:
        out.append(_render_segment(seg, zoneset, drill_map))
    out.append("")

    if workout.get("equipment"):
        out.append(f"**Utrustning:** {', '.join(workout['equipment'])}")
        out.append("")

    if workout.get("abort_conditions"):
        out.append("**Avbryta om:**")
        for cond in workout["abort_conditions"]:
            out.append(f"- {cond}")
        out.append("")

    if workout.get("coach_notes"):
        out.append("**Tränarens noter:**")
        out.append(workout["coach_notes"].strip())
        out.append("")

    out.extend(_render_drill_quick_ref(workout, drill_map))

    return "\n".join(out).rstrip() + "\n"
