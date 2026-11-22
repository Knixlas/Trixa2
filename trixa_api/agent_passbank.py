"""Passbanken och motorns förslag för AI-coachen (MCP / ``/agent/*``).

Förut kunde en AI-coach via MCP bara läsa data och skriva fri text:
``plan_session`` tog emot vad som helst, ``workout_code`` kontrollerades inte,
och varken passbanken, motorns veckoförslag eller nivåns gränser gick att
läsa. Coachens pass vinner alltid över motorn, så en coach kunde ge en
nybörjare fem långa pass på fackspråk — precis det docs/13 rättade i motorn.

Här finns tre saker:
  - ``engine_week``: motorns torrkörda vecka med nivåns gränser.
  - ``passbank_list``: passbanken filtrerad på gren, kategori, fas och nivå.
  - ``build_from_passbank`` + ``level_warnings``: ett pass med kod får sin
    text, sina steg och sin längd ur passbanken; varje pass prövas mot
    nivåns tak, budget och tillåtna kategorier.

Varningar, inte avslag: coachen får avvika, men ska då säga varför med
log_override. Det är samma manual_override-praxis som CLAUDE.md beskriver.
"""

from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Any

from fastapi import HTTPException

from coach.trixa import clock, levels, sports

# Kategorikoderna med adeptens ord (samma som veckovyn i ui).
CATEGORY_LABELS = {
    "AE": "Lugn uthållighet", "TE": "Tempo", "MF": "Kraft", "SS": "Snabbhet",
    "ME": "Tröskel", "AC": "Intervaller", "T": "Test", "BW": "Kombinationspass",
    "ST": "Styrka",
}

_NO_PASSBANK_NEEDED = {"rest", "yoga", "walk"}


def _pool() -> dict[str, dict]:
    from coach.engine.loader import load_workouts

    return {w["code"]: w for w in load_workouts()}


def _duration_spec(w: dict) -> dict:
    """{default, range} för passets längd, ur parametern eller uppskattningen."""
    spec = (w.get("parameters") or {}).get("duration_min")
    if isinstance(spec, dict):
        rng = spec.get("range") or [spec.get("min"), spec.get("max")]
        return {"default": spec.get("default"),
                "range": [r for r in rng if r is not None] or None}
    td = w.get("total_duration_min") or {}
    est = td.get("estimated")
    return {"default": est if isinstance(est, (int, float)) else None,
            "range": td.get("flexible_range")}


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    for stop in (". ", "! ", "? "):
        if stop in text:
            return text.split(stop, 1)[0] + stop.strip()
    return text


def _progress_weeks(athlete: dict) -> int:
    ramp = (athlete.get("phase_state") or {}).get("ramp") or {}
    try:
        return int(ramp.get("progress_weeks") or 0)
    except (TypeError, ValueError):
        return 0


def _next_race(client, athlete: dict) -> dict | None:
    try:
        from coach.trixa.races import fetch_next_a_race

        return fetch_next_a_race(client, athlete.get("id"), clock.today())
    except Exception:  # noqa: BLE001 — utan races-tabell gäller bara nivåns tak
        return None


# ---------- motorns vecka ----------


def engine_week(user_id: str, monday: date_type) -> dict:
    """Motorns förslag för veckan, torrkört — ingenting sparas.

    Dagar som coachen eller adepten redan planerat hoppar motorn över
    (``coached_dates``); övriga dagar visar vad motorn skulle lägga.
    """
    from coach.trixa.planner import generate_week

    plan = generate_week(user_id, monday, dry_run=True)
    d = plan.engine_decisions
    return {
        "week_start": monday.isoformat(),
        "saved": False,
        "phase": plan.phase,
        "period": plan.period,
        "phase_reason": (d.get("phase_recommendation") or {}).get("reason"),
        "allowed_categories": [
            {"code": c, "label": CATEGORY_LABELS.get(c, c)} for c in plan.categories
        ],
        "level_limits": d.get("level_limits"),
        "ramp": d.get("ramp"),
        "volume_factor": d.get("_volume_factor"),
        "race_day": d.get("race_day"),
        "coached_dates": d.get("_coached_dates") or [],
        "sessions": [
            {
                "date": w.date.isoformat(),
                "sport": w.sport,
                "workout_code": w.code if w.workout_data else "",
                "title": w.title,
                "category": w.category,
                "category_label": CATEGORY_LABELS.get(w.category, w.category),
                "duration_min": w.duration_minutes,
                "intensity": w.intensity,
            }
            for w in plan.workouts
        ],
        "warnings": plan.warnings,
        "how_to_use": (
            "Det här är motorns förslag inom adeptens nivå, budget och fas. "
            "Vill du lägga ett pass ur förslaget: plan_session med dess "
            "workout_code. Avviker du (annat pass, längre, hårdare): "
            "dokumentera med log_override."
        ),
    }


# ---------- passbanken ----------


def passbank_list(
    athlete: dict, sport: str | None = None, category: str | None = None,
    phase: str | None = None,
) -> dict:
    """Passbanken filtrerad på gren, kategori, fas och adeptens nivå."""
    prof = levels.profile(athlete)
    progress = _progress_weeks(athlete)
    sport_key = sports.canon(sport) if sport else None
    if sport and sport_key is None:
        raise HTTPException(400, f"Okänd gren: {sport!r}.")
    out = []
    for w in _pool().values():
        if sport_key and w.get("discipline") != sport_key:
            continue
        if category and w.get("category") != category:
            continue
        if phase and not any(str(p).startswith(phase) for p in w.get("phase_appropriate") or []):
            continue
        only_for = w.get("levels")
        if only_for and prof["level"] not in only_for:
            continue
        allowed = bool(levels.allowed_categories(prof, [w.get("category")], progress)) \
            or w.get("category") == "ST"
        out.append({
            "workout_code": w["code"],
            "sport": w.get("discipline"),
            "category": w.get("category"),
            "category_label": CATEGORY_LABELS.get(w.get("category"), w.get("category")),
            "name": w.get("name"),
            "summary": _first_sentence(w.get("intent") or ""),
            "duration_min": _duration_spec(w),
            "phases": w.get("phase_appropriate") or [],
            "equipment": w.get("equipment") or [],
            "allowed_for_level": allowed,
        })
    out.sort(key=lambda r: (r["sport"] or "", r["category"] or "", r["workout_code"]))
    return {"level": prof["level"], "count": len(out), "workouts": out}


def build_from_passbank(
    athlete: dict, code: str, sport_key: str, duration_min: int | None,
    coach_details: str,
) -> tuple[dict, list[str]]:
    """Passet ur passbanken: adeptens text, steg, övningar, längd och intensitet.

    Okänd kod eller fel gren avvisas — en kod som inte finns i banken gav
    förut ett pass med en etikett som inte betydde något. Längd utanför
    mallens spann flyttas in i spannet, med en varning.
    """
    from coach.engine.loader import load_drills, load_strength_exercises
    from coach.engine.profile import profile_from_athlete_row
    from coach.engine.renderer import render_workout
    from coach.engine.templates import resolve_template
    from coach.trixa.exercise_plan import exercises_from_steps
    from coach.trixa.planner import _fmt_minutes

    w = _pool().get(code)
    if w is None:
        raise HTTPException(
            400, f"Okänd passkod {code!r}. Hämta koderna med list_workouts eller get_engine_week."
        )
    disc = w.get("discipline")
    if disc != sport_key and not (disc == "brick" and sport_key in ("bike", "run", "brick")):
        raise HTTPException(
            400, f"Passet {code} är {disc}, inte {sport_key}. Skicka sport={disc}."
        )
    warnings: list[str] = []
    prof = levels.profile(athlete)
    only_for = w.get("levels")
    if only_for and prof["level"] not in only_for:
        warnings.append(
            f"Passet {code} är skrivet för nivån {', '.join(only_for)}, adepten är {prof['level']}."
        )

    params: dict[str, Any] = {}
    minutes = duration_min
    spec = (w.get("parameters") or {}).get("duration_min")
    if w.get("parameterized") and isinstance(spec, dict):
        rng = spec.get("range") or [spec.get("min"), spec.get("max")]
        lo, hi = (rng[0], rng[-1]) if rng and None not in rng else (None, None)
        minutes = int(minutes or spec.get("default") or 60)
        if lo is not None and minutes < lo:
            warnings.append(f"{minutes} min är kortare än passets spann ({lo}–{hi}); satt till {lo}.")
            minutes = int(lo)
        if hi is not None and minutes > hi:
            warnings.append(f"{minutes} min är längre än passets spann ({lo}–{hi}); satt till {hi}.")
            minutes = int(hi)
        params["duration_min"] = minutes
    if code == "AE1_run_gajogg":
        jog, walk = levels.run_walk(prof, _progress_weeks(athlete)) or (10.0, 1.0)
        params.update(jog_min=_fmt_minutes(jog), walk_min=_fmt_minutes(walk))

    resolved = resolve_template(w, params) if w.get("parameterized") else w
    if not minutes:
        try:
            minutes = int((resolved.get("total_duration_min") or {}).get("estimated") or 0) or None
        except (TypeError, ValueError):
            minutes = None

    drills = {d["code"]: d for d in load_drills()}
    exercise_map = {e["code"]: e for e in load_strength_exercises()}
    text = render_workout(resolved, profile_from_athlete_row(athlete), drills, exercise_map)
    if coach_details.strip():
        text = text.rstrip() + "\n\n**Från din coach:**\n" + coach_details.strip() + "\n"

    steps = resolved.get("main_set") or []
    reps_range = (resolved.get("parameters") or {}).get("reps")
    exercises = exercises_from_steps(steps, exercise_map, reps_range) if disc == "strength" else []
    zones = resolved.get("zone_refs") or []
    return {
        "title": w.get("name") or code,
        "details": text,
        "steps": steps,
        "exercises": exercises or None,
        "duration_min": minutes,
        "intensity": "Styrka" if disc == "strength" else ", ".join(str(z) for z in zones),
        "category": w.get("category"),
    }, warnings


def level_warnings(
    client, athlete: dict, user_id: str, day: date_type, sport_key: str,
    duration_min: int | None, category: str | None, from_passbank: bool,
) -> list[str]:
    """Pröva ett coachpass mot nivån: passets längd, kategori och veckans budget."""
    if sport_key in _NO_PASSBANK_NEEDED:
        return []
    prof = levels.profile(athlete)
    level = prof["level"]
    out: list[str] = []
    override = " Gör du det ändå: säg varför med log_override."

    # Styrka med coachens egen övningslista är ett legitimt coachpass
    # (rehab, maskinprogram); påminnelsen gäller konditionspassen.
    if not from_passbank and sport_key != "strength":
        out.append(
            "Passet kommer inte ur passbanken, så adepten får din text som den "
            "står och Trixa kan inte pröva passtypen mot nivån. Hitta ett pass "
            "med list_workouts eller get_engine_week och skicka dess workout_code."
        )

    race = _next_race(client, athlete)
    cap = levels.session_cap(prof, sport_key, (race or {}).get("distance"))
    if duration_min and cap and duration_min > cap:
        out.append(
            f"Passet är {duration_min} min. För nivån {level} är {cap} min det "
            f"längsta passet i grenen.{override}"
        )

    if category and category != "ST":
        if not levels.allowed_categories(prof, [category], _progress_weeks(athlete)):
            label = CATEGORY_LABELS.get(category, category)
            out.append(
                f"Passtypen {label} ({category}) ingår inte i nivån {level} ännu "
                f"— motorn ger bara lugn uthållighet tills grunden finns.{override}"
            )

    if levels.is_ramp(prof):
        monday = day - timedelta(days=day.weekday())
        rows = (
            client.table("planned_sessions")
            .select("date, sport, duration_min, status")
            .eq("user_id", user_id)
            .gte("date", monday.isoformat())
            .lte("date", (monday + timedelta(days=6)).isoformat())
            .execute()
        ).data or []
        others = [
            r for r in rows
            if r.get("status") != "cancelled"
            and not sports.is_rest(sports.canon(r.get("sport")))
            and sports.is_training(sports.canon(r.get("sport")))
            and not (str(r.get("date"))[:10] == day.isoformat()
                     and sports.canon(r.get("sport")) == sport_key)
        ]
        total = sum(int(r.get("duration_min") or 0) for r in others) + int(duration_min or 0)
        ramp = (athlete.get("phase_state") or {}).get("ramp") or {}
        budget = ramp.get("budget_minutes") or prof.get("start_minutes_per_week")
        if budget and total > float(budget) * 1.10:
            out.append(
                f"Veckan blir {total} min. Adeptens budget (nivån {level}) är "
                f"{int(budget)} min och växer med högst 10 % i veckan när "
                f"passen blir gjorda.{override}"
            )
        # Antalet gäller konditionspass, som i motorn; styrkan räknas i budgeten.
        max_sessions = prof.get("max_sessions")
        endurance = sum(1 for r in others if sports.canon(r.get("sport")) != "strength")
        endurance += 0 if sport_key == "strength" else 1
        if max_sessions and sport_key != "strength" and endurance > int(max_sessions):
            out.append(
                f"Veckan får {endurance} konditionspass. Nivån {level} har högst "
                f"{max_sessions} i veckan.{override}"
            )
    return out
