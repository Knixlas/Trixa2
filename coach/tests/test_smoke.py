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
last_week = select_workout_types('base', 'base_2', 6, 6)
mid_week = select_workout_types('base', 'base_2', 3, 6)
base1_mid = select_workout_types('base', 'base_1', 3, 6)
print(f"  Sista veckan:    {last_week}")
print(f"  Mitt i perioden: {mid_week}")
print(f"  base_1 mitt i:   {base1_mid}")
assert "TE" in mid_week, "TE ska vara valbar mitt i base_2 (tillagd 2026-07-02)"
assert "TE" not in last_week and "MF" not in last_week, (
    "Sista veckan är återhämtningsvecka — TE/MF ska exkluderas (exclude_last_week)"
)
assert "TE" not in base1_mid, "TE hör inte hemma i base_1"
build_mid = select_workout_types('build', 'build_1', 2, 4)
assert "TE" in build_mid, "TE ska vara valbar i build (ej sista veckan)"
peak_types = select_workout_types('peak', None, 1, 3)
assert "AE" in peak_types and "SS" in peak_types, (
    "Peak ska innehålla AE + SS (taper = reducerad volym, bibehållen skärpa)"
)

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
prep_p = current_strength_protocol('prep')
mt_p = current_strength_protocol('base', 'base_1', 2, 6)
ms_p = current_strength_protocol('base', 'base_1', 5, 6)
sm_p = current_strength_protocol('build')
print(f"  prep:           {prep_p.protocol_code} ({prep_p.sets} set × {prep_p.reps} reps, {prep_p.sessions_per_week}/v)")
print(f"  base_1 v2 (MT): {mt_p.protocol_code}")
print(f"  base_1 v5 (MS): {ms_p.protocol_code} ({ms_p.sets} set × {ms_p.reps} reps, {ms_p.intensity})")
print(f"  build:          {sm_p.protocol_code} ({sm_p.sets} set × {sm_p.reps} reps, {sm_p.sessions_per_week}/v)")
assert (prep_p.protocol_code, mt_p.protocol_code, ms_p.protocol_code, sm_p.protocol_code) == (
    "AA", "MT", "MS", "SM"
)
# MS = maxstyrka: få reps, tungt. SM = underhåll: 1-2 set, 1 pass/vecka.
# (Var inverterat före 2026-07-02 — MS hade 10-12 reps light, SM 4-5 set heavy.)
assert ms_p.reps == (3, 6) and ms_p.intensity == "heavy", "MS ska vara maxstyrka"
assert sm_p.sets == (1, 2) and sm_p.sessions_per_week == (1, 1), "SM ska vara underhåll"
assert prep_p.sessions_per_week == (2, 3), "AA ska ha 2-3 pass/vecka"

# ---- Test 7: överträning ----

divider("10. Överträning — tidiga tecken")
a = assess_overtraining(OvertrainingSignals(
    rhr_bpm_over_baseline=6,
    motivation_low=True,
))
print(f"  {a.level}: {a.label} (flags: {a.flag_count}, viktade: {a.weighted_count})")
assert a.level == "early", "2 svaga flaggor utan severe ska ge early"
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
print(f"  {a.level}: {a.label} (flags: {a.flag_count}, viktade: {a.weighted_count})")
assert a.level == "severe", "7 flaggor varav 2 severe ska ge severe"
adj = recommend_adjustment(a)
print(f"  → volym -{adj.volume_reduction_pct}%, +{adj.extra_rest_days} vilodagar")
print(f"  → läkarkontakt: {adj.consider_medical_consultation}")

divider("12. Överträning — severe-viktning (severe väger dubbelt)")
a = assess_overtraining(OvertrainingSignals(
    rhr_bpm_over_baseline=12,      # severe (vikt 2)
    hrv_pct_below_baseline=25,     # severe (vikt 2)
    motivation_low=True,           # vanlig (vikt 1)
))
print(f"  {a.level}: 3 flaggor varav 2 severe → viktade {a.weighted_count}")
assert a.weighted_count == 5 and a.level == "severe", (
    "3 flaggor varav 2 severe = viktat 5 → severe"
)
a = assess_overtraining(OvertrainingSignals(
    rhr_bpm_over_baseline=6,
    hrv_pct_below_baseline=12,
    sleep_score_avg_7d=55,
    motivation_low=True,
))
print(f"  {a.level}: 4 vanliga flaggor → viktade {a.weighted_count}")
assert a.weighted_count == 4 and a.level == "moderate", "4 vanliga flaggor → moderate"

print("\n✓ ALLT GRÖNT")
