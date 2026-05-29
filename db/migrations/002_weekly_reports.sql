-- 002_weekly_reports
--
-- Strukturerad veckorapport som adept fyller i (söndag eller måndag).
-- Ersätter fritext-mående i gamla profiles.health_notes. Trixa läser
-- detta som input till engine; Nils läser också (kan tolka fritext-fält).
--
-- Skattningar 1-5 (5 = bäst):
--   sleep_quality, motivation, soreness (5 = ingen ömhet),
--   energy, stress (5 = lågt)
--
-- Booleska flaggor som triggar mer detaljerade frågor i UI:t.
-- pain_locations: {location, severity (1-5), affects_disciplines, since}.

CREATE TABLE IF NOT EXISTS public.weekly_reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  athlete_id uuid NOT NULL REFERENCES public.athlete_profiles(id) ON DELETE CASCADE,
  week_start date NOT NULL,

  sleep_quality int CHECK (sleep_quality BETWEEN 1 AND 5),
  motivation int CHECK (motivation BETWEEN 1 AND 5),
  soreness int CHECK (soreness BETWEEN 1 AND 5),
  energy int CHECK (energy BETWEEN 1 AND 5),
  stress int CHECK (stress BETWEEN 1 AND 5),

  pain_present boolean NOT NULL DEFAULT false,
  injury_change boolean NOT NULL DEFAULT false,
  illness_present boolean NOT NULL DEFAULT false,
  travel_planned boolean NOT NULL DEFAULT false,

  pain_locations jsonb NOT NULL DEFAULT '[]'::jsonb,

  notes text DEFAULT '',

  submitted_at timestamptz NOT NULL DEFAULT now(),

  UNIQUE(athlete_id, week_start)
);

CREATE INDEX IF NOT EXISTS weekly_reports_athlete_week_idx
  ON public.weekly_reports(athlete_id, week_start DESC);

ALTER TABLE public.weekly_reports ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Adepter hanterar sina egna veckorapporter"
  ON public.weekly_reports FOR ALL
  USING (
    EXISTS (
      SELECT 1 FROM public.athlete_profiles ap
      WHERE ap.id = weekly_reports.athlete_id
        AND ap.user_id = auth.uid()
    )
  );

CREATE POLICY "Coach läser sina adepters veckorapporter"
  ON public.weekly_reports FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.athlete_profiles ap
      JOIN public.coach_athletes ca ON ca.athlete_id = ap.user_id
      WHERE ap.id = weekly_reports.athlete_id
        AND ca.coach_id = auth.uid()
        AND ca.status = 'active'
    )
  );

COMMENT ON TABLE public.weekly_reports IS
  'Veckorapport-formulär från adept till Trixa. En rad per (athlete, week_start).';
