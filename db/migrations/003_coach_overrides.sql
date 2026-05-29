-- 003_coach_overrides
--
-- Strukturerad override-logg. När Nils (LLM-coach) avviker från Trixas
-- engine-beslut loggas det här med fullständigt spårbarhetsskäl.
-- Trixa-planner läser aktiv override INNAN nästa vecka genereras —
-- override är förstklassig coach-handling, inte undantag.
--
-- Scopes:
--   week         — gäller hela veckan (fas, volym, kategorier)
--   workout      — gäller ett specifikt pass
--   phase        — adept stannar i / går till en annan fas än engine säger
--   volume       — annan total veckotimme än engine räknat ut
--   overtraining — annan OT-bedömning än engine

CREATE TABLE IF NOT EXISTS public.coach_overrides (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  athlete_id uuid NOT NULL REFERENCES public.athlete_profiles(id) ON DELETE CASCADE,
  coach_user_id uuid NOT NULL REFERENCES auth.users(id),

  scope text NOT NULL CHECK (scope IN ('week', 'workout', 'phase', 'volume', 'overtraining')),
  week_id uuid REFERENCES public.training_weeks(id) ON DELETE CASCADE,
  workout_id uuid REFERENCES public.workouts(id) ON DELETE CASCADE,

  engine_recommendation jsonb NOT NULL,
  override_decision jsonb NOT NULL,
  motivation text NOT NULL CHECK (length(trim(motivation)) >= 10),

  medical_context_disclosed boolean NOT NULL DEFAULT false,
  athlete_explicit_request boolean NOT NULL DEFAULT false,

  is_active boolean NOT NULL DEFAULT true,

  created_at timestamptz NOT NULL DEFAULT now(),

  honored_by_planner boolean NOT NULL DEFAULT false,
  honored_at timestamptz,

  CONSTRAINT scope_matches_target CHECK (
    (scope = 'workout' AND workout_id IS NOT NULL)
    OR (scope = 'week' AND week_id IS NOT NULL)
    OR scope IN ('phase', 'volume', 'overtraining')
  )
);

CREATE INDEX IF NOT EXISTS coach_overrides_athlete_active_idx
  ON public.coach_overrides(athlete_id, is_active)
  WHERE is_active = true;

CREATE INDEX IF NOT EXISTS coach_overrides_week_idx
  ON public.coach_overrides(week_id)
  WHERE week_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS coach_overrides_workout_idx
  ON public.coach_overrides(workout_id)
  WHERE workout_id IS NOT NULL;

ALTER TABLE public.coach_overrides ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Coach och adept hanterar sina overrides"
  ON public.coach_overrides FOR ALL
  USING (
    coach_user_id = auth.uid()
    OR EXISTS (
      SELECT 1 FROM public.athlete_profiles ap
      WHERE ap.id = coach_overrides.athlete_id
        AND ap.user_id = auth.uid()
    )
  );

COMMENT ON TABLE public.coach_overrides IS
  'Coach-override av engine-rekommendation. Trixa-planner respekterar aktiv override när nästa vecka genereras.';
COMMENT ON COLUMN public.coach_overrides.motivation IS
  'Klartext-motivering. Minst 10 tecken — tom motivering är inte tillåten.';
COMMENT ON COLUMN public.coach_overrides.honored_by_planner IS
  'Trixa-planner sätter denna när override har införlivats i en konkret veckoplan.';
