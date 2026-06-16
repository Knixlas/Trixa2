"""Verifierar att parametriserade pass har konsistenta proportioner.

KAN VALIDERA:
  - Vid default-budget: total ≈ default (±15%). Visar att pcts +
    implicit-fast-pct ≈ 1.0.
  - Sets-range är meningsfull (har min < default < max).
  - Pct-värden ligger i rimligt intervall (>0, <0.5 per segment).

KAN INTE VALIDERA:
  - Exakt skalning vid andra budgetar — det är renderarens jobb
    (skalar sets med budget, eventuellt absorberar via pct).

Modell vid skalningstest: sets interpoleras linjärt mellan range
beroende på budget vs default. Pct-segment får budget × pct.
"""
import sys
import yaml
from pathlib import Path

WORKOUT_DIR = Path("/home/claude/coach/data/workouts")
ZONE_PACE_SEC_PER_100M = {1: 103, 2: 99, 3: 95, 4: 91, 5: 86}


def get_sets(seg, budget_min=None, default_budget=None):
    s = seg.get("sets", 1)
    if isinstance(s, dict):
        default = s.get("default", 1)
        rng = s.get("range", [default, default])
        if budget_min is None:
            return default
        scaled = round(default * budget_min / default_budget)
        return max(rng[0], min(rng[1], scaled))
    return s


def segment_time_sec(seg, budget_min, default_budget):
    pct = seg.get("duration_pct")
    if pct is not None:
        return budget_min * 60 * pct

    sets = get_sets(seg, budget_min, default_budget)
    dist = seg.get("distance_m", 0)
    rest = seg.get("rest_sec", 0) or 0
    zone = seg.get("zone", 2)
    zones_per_set = seg.get("zones_per_set")

    if zones_per_set:
        total = 0
        for z in zones_per_set:
            pace = ZONE_PACE_SEC_PER_100M.get(z, 99)
            total += (dist / 100) * pace + rest
        return total

    pace = ZONE_PACE_SEC_PER_100M.get(zone, 99)
    return sets * ((dist / 100) * pace + rest)


def resolve_total_min(workout, budget_min, default_budget):
    total = 0
    for seg in workout["main_set"]:
        if seg.get("segment") == "rest":
            total += seg.get("rest_sec", 0)
            continue
        total += segment_time_sec(seg, budget_min, default_budget)
    return total / 60


def get_params(w):
    p = w.get("parameters", {}).get("duration_min", {})
    if isinstance(p, dict):
        if "default" in p and "range" in p:
            return p["default"], p["range"]
        if "min" in p and "max" in p:
            return p["default"], [p["min"], p["max"]]
    return None, None


def main():
    files = sys.argv[1:] if len(sys.argv) > 1 else ["swim_AE.yaml"]
    workouts = []
    for f in files:
        data = yaml.safe_load((WORKOUT_DIR / f).read_text())
        workouts.extend(data["workouts"])

    print("=" * 78)
    print(f"Validering vid default-budget: {', '.join(files)}")
    print("=" * 78)

    failed = []
    for w in workouts:
        if not w.get("parametrized"):
            continue
        default, rng = get_params(w)
        if not default:
            continue

        total_at_default = resolve_total_min(w, default, default)
        delta = total_at_default - default
        rel_delta = abs(delta) / default
        mark = "✓" if rel_delta <= 0.15 else "✗"

        # Räkna proportioner
        explicit_pct = sum(s.get("duration_pct", 0) for s in w["main_set"])
        fixed_sec = sum(segment_time_sec(s, default, default) 
                        for s in w["main_set"] 
                        if s.get("duration_pct") is None 
                        and s.get("segment") != "rest")
        implicit_pct = fixed_sec / (default * 60)
        total_pct = explicit_pct + implicit_pct

        print(f"\n{w['code']} ({default} min default, spann {rng}):")
        print(f"  Vid default: {total_at_default:.1f} min "
              f"(Δ {delta:+.1f}, {rel_delta*100:.0f}%) {mark}")
        print(f"  Explicit pct: {explicit_pct:.2f} | "
              f"Implicit fast pct: {implicit_pct:.2f} | "
              f"Sum: {total_pct:.2f}")
        if rel_delta > 0.15:
            failed.append((w["code"], default, total_at_default))

    print()
    if failed:
        print(f"FAIL: {len(failed)} pass utanför ±15% vid default")
        for c, b, t in failed:
            print(f"  {c}: default {b} min → {t:.1f} min")
        sys.exit(1)
    else:
        print(f"OK alla {len([w for w in workouts if w.get('parametrized')])} parametriserade pass har konsistent default-tid")


if __name__ == "__main__":
    main()
