"""Smoke-test för coach.engine.

Körs från Trixa2-roten:
    python -m tests.test_smoke
"""

from coach.engine.phases import determine_phase, AthleteState
from coach.engine.workouts import (
    select_workout_types,
    distribute_weekly_hours,
    max_session_minutes,
    hard_training_cap_minutes,
)
from coach.engine.strength import current_strength_protocol
from coach.engine.overtraining import (
    OvertrainingSignals,
    assess_overtraining,
    recommend_adjustment,
)


def divider(s):
    print(f"\n{'=' * 60}\n{s}\n{'=' * 60}")


# ---- Test 1: fasidentifiering (optimal-modell: optimal capad av readiness) ----

divider("1. Låg volym, ingen tävling → prep (capad)")
rec = determine_phase(AthleteState(weekly_training_hours=3.0))
print(f"  → {rec.phase} (optimal {rec.optimal_phase}, behind={rec.behind}): {rec.reason}")

divider("2. Tävling 11v bort, 8.4h volym → build (Johan)")
rec = determine_phase(AthleteState(
    weekly_training_hours=8.4,
    weeks_until_next_race=11,
))
print(f"  → {rec.phase} ({rec.period}, optimal {rec.optimal_phase}, behind={rec.behind}): {rec.reason}")
assert rec.phase == "build" and not rec.behind, "Johan borde hamna i build utan behind"

divider("3. Tävling 11v bort, 1.6h volym → prep + behind (Niklas)")
rec = determine_phase(AthleteState(
    weekly_training_hours=1.6,
    weeks_until_next_race=11,
))
print(f"  → {rec.phase} (optimal {rec.optimal_phase}, behind={rec.behind}): {rec.reason}")
print(f"  Brister: {rec.unmet_criteria}")
assert rec.phase == "prep" and rec.optimal_phase == "build" and rec.behind, "Niklas borde capas till prep + behind"

divider("4. Tävling 3v bort → peak")
rec = determine_phase(AthleteState(
    weekly_training_hours=10.0,
    weeks_until_next_race=3,
))
print(f"  → {rec.phase} (optimal {rec.optimal_phase}): {rec.reason}")
assert rec.phase == "peak", "3v bort borde ge peak"

# ---- Test 2: passtyper ----

divider("5. Passtyper i base_2")
print(f"  Sista veckan:    {select_workout_types('base', 'base_2', 6, 6)}")
print(f"  Mitt i perioden: {select_workout_types('base', 'base_2', 3, 6)}")
print(f"  base_1 mitt i:   {select_workout_types('base', 'base_1', 3, 6)}")

# ---- Test 3: volymfördelning ----

divider("6. 10h fördelat på discipliner (build)")
print(f"  {distribute_weekly_hours('build', 10.0)}")

# ---- Test 4: passlängd ----

divider("7. Max passlängd")
print(f"  prep run:    {max_session_minutes('prep', 'run')} min")
print(f"  build bike:  {max_session_minutes('build', 'bike')} min")

# ---- Test 5: hård träning ----

divider("8. Hård-träning-tak")
print(f"  prep (600 min total):")
print(f"    {hard_training_cap_minutes('prep', 600.0)}")
print(f"  base (förra v: 60 min hård):")
print(f"    {hard_training_cap_minutes('base', 600.0, previous_week_hard_minutes=60.0)}")

# ---- Test 6: styrkeprotokoll ----

divider("9. Styrkeprotokoll")
print(f"  prep:           {current_strength_protocol('prep').protocol_code}")
print(f"  base_1 v2 (MT): {current_strength_protocol('base', 'base_1', 2, 6).protocol_code}")
print(f"  base_1 v5 (MS): {current_strength_protocol('base', 'base_1', 5, 6).protocol_code}")
print(f"  build:          {current_strength_protocol('build').protocol_code}")

# ---- Test 7: överträning ----

divider("10. Överträning — tidiga tecken")
a = assess_overtraining(OvertrainingSignals(
    rhr_bpm_over_baseline=6,
    motivation_low=True,
))
print(f"  {a.level}: {a.label} (flags: {a.flag_count})")
adj = recommend_adjustment(a)
print(f"  → volym -{adj.volume_reduction_pct}%, intensitet -{adj.intensity_reduction_pct}%")

divider("11. Överträning — allvarligt")
a = assess_overtraining(OvertrainingSignals(
    rhr_bpm_over_baseline=12,
    hrv_pct_below_baseline=25,
    sleep_score_avg_7d=50,
    motivation_low=True,
    irritability=True,
    muscle_fatigue_persistent=True,
    poor_recovery=True,
))
print(f"  {a.level}: {a.label} (flags: {a.flag_count})")
adj = recommend_adjustment(a)
print(f"  → volym -{adj.volume_reduction_pct}%, +{adj.extra_rest_days} vilodagar")
print(f"  → läkarkontakt: {adj.consider_medical_consultation}")

print("\n✓ ALLT GRÖNT")
