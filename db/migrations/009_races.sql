-- 009_races
--
-- Tävlingskalender per adept (ersätter coach/data/races.yaml som var
-- Niklas-specifik data i en generell fil). Planner läser "nästa A-race"
-- via coach/trixa/races.py::fetch_next_a_race med fallback till
-- athlete_profiles.race_date (behålls som denormaliserad reserv).
--
-- target_total/result_total som text 'HH:MM:SS' — postgrest-vänligt och
-- matchar time_goal-stilen i athlete_profiles.

CREATE TABLE IF NOT EXISTS public.races (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  athlete_id uuid NOT NULL REFERENCES public.athlete_profiles(id) ON DELETE CASCADE,
  name text NOT NULL,
  date date NOT NULL,
  distance text NOT NULL CHECK (distance IN ('sprint', 'olympic', 'half', 'full')),
  priority text NOT NULL DEFAULT 'B' CHECK (priority IN ('A', 'B', 'C')),
  target_total text,
  targets jsonb NOT NULL DEFAULT '{}'::jsonb,
  result_total text,
  notes text DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS races_athlete_date_idx
  ON public.races (athlete_id, date);

ALTER TABLE public.races ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS races_own_rows ON public.races;
CREATE POLICY races_own_rows ON public.races FOR ALL
  USING (athlete_id IN (
    SELECT id FROM public.athlete_profiles WHERE user_id = auth.uid()
  ))
  WITH CHECK (athlete_id IN (
    SELECT id FROM public.athlete_profiles WHERE user_id = auth.uid()
  ));

COMMENT ON TABLE public.races IS
  'Tävlingskalender per adept. A/B/C-prioritering enligt Friel. Planner räknar fas från nästa A-race.';

-- Seed: Niklas (athlete_profiles.id = 81b667bc-f37c-4311-a45e-1b0a28d1ada7).
-- Mål från athlete_profiles.time_goal (sub-13:00) — races.yaml-målet 11:30
-- var stale och inkonsistent med tröskelvärdena (expertgranskning 2026-07-02).
INSERT INTO public.races (athlete_id, name, date, distance, priority, target_total, targets, result_total, notes)
SELECT * FROM (VALUES
  (
    '81b667bc-f37c-4311-a45e-1b0a28d1ada7'::uuid,
    'Ironman Kalmar',
    '2026-08-15'::date,
    'full', 'A', '13:00:00',
    '{"swim": "1:30:00", "bike": "6:30:00", "run": "4:30:00"}'::jsonb,
    NULL,
    'Huvudtävling 2026. Taper 3 v. Mål synkat från athlete_profiles.time_goal.'
  ),
  (
    '81b667bc-f37c-4311-a45e-1b0a28d1ada7'::uuid,
    'Ironman (2025)',
    '2025-09-20'::date,
    'full', 'C', NULL,
    '{}'::jsonb,
    '13:42:00',
    'Historik — multi-sport-passet med 225 km synkat från Garmin.'
  )
) AS seed(athlete_id, name, date, distance, priority, target_total, targets, result_total, notes)
WHERE NOT EXISTS (
  SELECT 1 FROM public.races r
  WHERE r.athlete_id = seed.athlete_id AND r.date = seed.date
);
