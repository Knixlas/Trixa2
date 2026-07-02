-- 008_athlete_profiles_user_fields
--
-- Multi-tenant-separationen (2026-07-02): allt adept-specifikt som tidigare
-- bodde i coach/data/athlete_config.yaml flyttar in i athlete_profiles.
-- Engine/planner läser profilen per user_id via
-- coach/engine/profile.py::profile_from_athlete_row.
--
-- Nya fält:
--   max_hr / resting_hr    — pulsreferenser (statiska; dynamisk RHR-baseline
--                            kommer fortsatt från garmin_coach.daily_metrics)
--   lthr_bike              — cykel-LTHR, separat från lthr (löp-AT)
--   threshold_meta         — {"ftp":{"source":"estimate|test|race",
--                            "tested_at":"YYYY-MM-DD"}, "css":{...},
--                            "run_pace":{...}} — ålder/kvalitet per värde,
--                            driver stale_test_values-alerten
--   recovery_week_ratio    — '3:1' (default) eller '2:1' (masters):
--                            styr vilovecko-cykelns längd i planner
--   race_carbs_per_hour_g  — per-adept-override av race-nutrition
--   carb_load_g_per_kg     — per-adept-override av kolhydratladdning
--   nutrition_notes        — fritext (t.ex. GLP-1-begränsningar)
--   onboarded_at           — NULL tills onboarding-formuläret är ifyllt;
--                            dashboarden redirectar till /ui/onboarding

ALTER TABLE public.athlete_profiles
  ADD COLUMN IF NOT EXISTS max_hr int,
  ADD COLUMN IF NOT EXISTS resting_hr int,
  ADD COLUMN IF NOT EXISTS lthr_bike int,
  ADD COLUMN IF NOT EXISTS threshold_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS recovery_week_ratio text NOT NULL DEFAULT '3:1',
  ADD COLUMN IF NOT EXISTS race_carbs_per_hour_g int,
  ADD COLUMN IF NOT EXISTS carb_load_g_per_kg numeric(4,1),
  ADD COLUMN IF NOT EXISTS nutrition_notes text DEFAULT '',
  ADD COLUMN IF NOT EXISTS onboarded_at timestamptz;

ALTER TABLE public.athlete_profiles
  DROP CONSTRAINT IF EXISTS athlete_profiles_recovery_week_ratio_check;
ALTER TABLE public.athlete_profiles
  ADD CONSTRAINT athlete_profiles_recovery_week_ratio_check
  CHECK (recovery_week_ratio IN ('3:1', '2:1'));

COMMENT ON COLUMN public.athlete_profiles.max_hr IS
  'Maxpuls (bpm). Statisk referens för zonberäkning.';
COMMENT ON COLUMN public.athlete_profiles.resting_hr IS
  'Vilopuls (bpm), statisk referens. Dynamisk baseline: garmin_coach.daily_metrics.';
COMMENT ON COLUMN public.athlete_profiles.lthr_bike IS
  'Cykel-LTHR (bpm) — separat från lthr som är löp-AT.';
COMMENT ON COLUMN public.athlete_profiles.threshold_meta IS
  'Källa + testdatum per tröskelvärde: {"ftp":{"source":"estimate","tested_at":"2026-05-23"},...}';
COMMENT ON COLUMN public.athlete_profiles.recovery_week_ratio IS
  'Vilovecko-cykel: 3:1 (default) eller 2:1 (masters 50+). Styr planners periodräknare.';
COMMENT ON COLUMN public.athlete_profiles.race_carbs_per_hour_g IS
  'Per-adept race-nutrition (g kolhydrat/h). NULL = generell default i data/nutrition.yaml.';
COMMENT ON COLUMN public.athlete_profiles.carb_load_g_per_kg IS
  'Per-adept kolhydratladdning (g/kg/dag). NULL = generell default.';
COMMENT ON COLUMN public.athlete_profiles.nutrition_notes IS
  'Fritext-noteringar om nutrition (t.ex. GLP-1-begränsat intag under långpass).';
COMMENT ON COLUMN public.athlete_profiles.onboarded_at IS
  'När onboarding-formuläret fylldes i. NULL = redirect till /ui/onboarding.';

-- Backfill Niklas (användare 1) med värdena som tidigare bara fanns i
-- coach/data/athlete_config.yaml (estimat daterade 2026-05-23).
UPDATE public.athlete_profiles
SET max_hr = 185,
    resting_hr = 48,
    lthr_bike = 162,
    threshold_meta = '{
      "ftp": {"source": "estimate", "tested_at": "2026-05-23"},
      "css": {"source": "estimate", "tested_at": "2026-05-23"},
      "run_pace": {"source": "estimate", "tested_at": "2026-05-23"}
    }'::jsonb,
    recovery_week_ratio = '2:1',
    nutrition_notes = 'Ozempic: hård gräns för intag under långpass — race-nutrition måste tränas i långpass i god tid före tävling. Individualisera g/h nedåt från generell default.',
    onboarded_at = now()
WHERE user_id = '09db449d-b8fd-409a-b475-3401b0de9858';
