"""Readiness-projektion + ramp-vakt.

Skala upp nuvarande veckovolym med en SÄKER upptrappning och se när trenden
korsar varje fas tröskel (base ≥5 h/v, build ≥7 h/v — samma som engine.phases),
mot tävlingstajmingen. Plus en vakt som flaggar om den FAKTISKA upptrappningen
är för skarp (skaderisk).

Ren matte, ingen LLM. Bygger ovanpå optimal-plan-modellen (#2): projektionen
visar NÄR den volym-capade fasen kan släppa upp mot den race-optimala.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Fas-trösklar (h/v) — speglar phases.yaml.
_BASE_MIN = 5.0
_BUILD_MIN = 7.0

# Säkra default-konstanter (tunbara).
SAFE_RAMP = 0.10        # 10%-regeln: max veckoökning utan skaderisk
DELOAD_EVERY = 4        # var fjärde vecka = avlastning (3:1-stegring)
DELOAD_FACTOR = 0.80    # avlastningsvecka = 80% av trenden
BUILD_BLOCK_MIN_WEEKS = 4  # minst så många v build-volym före tapering för "i tid"

# Ramp-vakt (acute:chronic-aktig kvot på veckovolym).
RAMP_WARN = 1.30
RAMP_CRITICAL = 1.50


@dataclass
class ReadinessProjection:
    current_hours: float
    weeks_to_race: int | None
    ramp_pct: int
    base_eta: int | None       # veckor från nu till base-volym (0=redan, None=ej inom horisont)
    build_eta: int | None
    on_track: bool             # når build-volym i tid för en riktig byggfas?
    verdict: str
    curve: list = field(default_factory=list)  # [{week, hours, trend, deload}]


def project_weekly_hours(
    current_h: float,
    weeks_ahead: int,
    ramp: float = SAFE_RAMP,
    deload_every: int = DELOAD_EVERY,
    deload_factor: float = DELOAD_FACTOR,
) -> list[dict]:
    """Projicera veckovolym framåt: säker ramp på byggveckor, dipp på avlastning.

    `trend` är den sustainade (kroniska) nivån som fas-bedömningen följer;
    avlastningsveckans `hours` dippar men sänker inte trenden.
    """
    trend = float(current_h)
    out: list[dict] = []
    for w in range(1, max(weeks_ahead, 0) + 1):
        is_deload = bool(deload_every) and (w % deload_every == 0)
        if is_deload:
            shown = round(trend * deload_factor, 1)
        else:
            trend = round(trend * (1 + ramp), 1)
            shown = trend
        out.append({"week": w, "hours": shown, "trend": round(trend, 1), "deload": is_deload})
    return out


def _eta(curve: list[dict], current_h: float, threshold: float) -> int | None:
    if current_h >= threshold:
        return 0
    return next((p["week"] for p in curve if p["trend"] >= threshold), None)


def build_projection(
    current_h: float,
    weeks_to_race: int | None,
    ramp: float = SAFE_RAMP,
    deload_every: int = DELOAD_EVERY,
    deload_factor: float = DELOAD_FACTOR,
) -> ReadinessProjection:
    """Projicera readiness och bedöm om build-volym nås i tid till loppet."""
    horizon = weeks_to_race if (weeks_to_race and weeks_to_race > 0) else 16
    curve = project_weekly_hours(current_h, horizon, ramp, deload_every, deload_factor)
    base_eta = _eta(curve, current_h, _BASE_MIN)
    build_eta = _eta(curve, current_h, _BUILD_MIN)
    ramp_pct = round(ramp * 100)

    cur = round(float(current_h), 1)
    if weeks_to_race is None:
        on_track = current_h >= _BASE_MIN
        verdict = (
            f"Ingen tävling satt. Vid säker upptrappning (~{ramp_pct}%/v) bär du "
            + ("redan base-volym." if base_eta == 0 else f"base-volym om ~{base_eta} v."
               if base_eta else "inte base-volym inom horisonten.")
        )
        return ReadinessProjection(cur, None, ramp_pct, base_eta, build_eta, on_track, verdict, curve)

    if build_eta == 0:
        return ReadinessProjection(
            cur, weeks_to_race, ramp_pct, 0, 0, True,
            f"Du bär redan build-volym ({cur} h/v) — följ den optimala planen mot loppet.",
            curve,
        )

    margin = (weeks_to_race - build_eta) if build_eta is not None else None
    on_track = build_eta is not None and margin is not None and margin >= BUILD_BLOCK_MIN_WEEKS

    base_txt = "redan" if base_eta == 0 else (f"om ~{base_eta} v" if base_eta else "ej inom horisonten")
    if on_track:
        verdict = (
            f"Vid säker upptrappning (~{ramp_pct}%/v + avlastning) når du base {base_txt} "
            f"och build-volym om ~{build_eta} v — ~{margin} v marginal till loppet för en byggfas."
        )
    else:
        reach = f"om ~{build_eta} v" if build_eta is not None else "först bortom horisonten"
        if margin is not None and margin > 0:
            before = f"bara ~{margin} v före loppet"
        else:
            before = "i princip vid loppet"
        verdict = (
            f"Ligger efter: vid säker upptrappning når du build-volym {reach} — {before}. "
            f"För sent för en riktig byggfas; överväg ett senare eller kortare lopp."
        )
    return ReadinessProjection(cur, weeks_to_race, ramp_pct, base_eta, build_eta, on_track, verdict, curve)


def ramp_flag(recent_weekly_hours: list[float]) -> dict | None:
    """Flagga om FAKTISK upptrappning är för skarp (skaderisk).

    `recent_weekly_hours`: veckovolym äldst→nyast (minst 3 veckor inkl. den
    senaste). Jämför senaste veckan mot snittet av de föregående (acute:chronic).
    """
    weeks = [float(h) for h in recent_weekly_hours if h is not None]
    if len(weeks) < 3:
        return None
    last, prior = weeks[-1], weeks[:-1]
    chronic = sum(prior) / len(prior)
    if chronic <= 0:
        return None
    ratio = last / chronic
    pct = round((ratio - 1) * 100)
    if ratio >= RAMP_CRITICAL:
        return {"level": "critical", "ratio": round(ratio, 2),
                "msg": f"Volymen sköt upp {pct}% mot snittet — hög skaderisk. Backa ett snäpp den här veckan."}
    if ratio >= RAMP_WARN:
        return {"level": "warning", "ratio": round(ratio, 2),
                "msg": f"Volymen ökar snabbt (+{pct}% mot snittet) — håll igen så du inte trappar för skarpt."}
    return None
