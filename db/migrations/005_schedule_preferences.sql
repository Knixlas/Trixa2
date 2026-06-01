-- 005_schedule_preferences
--
-- Adept-val för veckans skelett: dag för långpass-cykel, dag för långpass-löp,
-- vilodagar. NULL/tom lista = "spelar ingen roll, systemet väljer".

ALTER TABLE public.athlete_profiles
  ADD COLUMN IF NOT EXISTS long_bike_day text
    CHECK (long_bike_day IS NULL OR long_bike_day IN
      ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')),
  ADD COLUMN IF NOT EXISTS long_run_day text
    CHECK (long_run_day IS NULL OR long_run_day IN
      ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')),
  ADD COLUMN IF NOT EXISTS preferred_rest_days jsonb NOT NULL DEFAULT '["monday"]'::jsonb;

COMMENT ON COLUMN public.athlete_profiles.long_bike_day IS
  'Dag för långpass-cykel. NULL = adept har inget preferens, systemet väljer (default saturday).';
COMMENT ON COLUMN public.athlete_profiles.long_run_day IS
  'Dag för långpass-löpning. NULL = adept har inget preferens, systemet väljer (default sunday).';
COMMENT ON COLUMN public.athlete_profiles.preferred_rest_days IS
  'Lista av veckodagar som adept föredrar som vilodagar. Default ["monday"]. Tom lista = ingen fast vilodag.';

-- Seed Niklas defaults
UPDATE public.athlete_profiles
SET long_bike_day = 'saturday',
    long_run_day = 'sunday',
    preferred_rest_days = '["monday"]'::jsonb
WHERE user_id = '09db449d-b8fd-409a-b475-3401b0de9858';
