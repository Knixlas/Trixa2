-- 008_master_tables_and_tp_tracking
--
-- Samlar den additiva livedatamodell som Trixa2-koden förutsätter.

ALTER TABLE public.planned_sessions
  ADD COLUMN IF NOT EXISTS workout_code text,
  ADD COLUMN IF NOT EXISTS intensity text,
  ADD COLUMN IF NOT EXISTS purpose text,
  ADD COLUMN IF NOT EXISTS origin text,
  ADD COLUMN IF NOT EXISTS steps jsonb,
  ADD COLUMN IF NOT EXISTS exercises jsonb,
  ADD COLUMN IF NOT EXISTS tp_workout_id bigint,
  ADD COLUMN IF NOT EXISTS tp_synced_hash text,
  ADD COLUMN IF NOT EXISTS tp_synced_at timestamptz;

CREATE INDEX IF NOT EXISTS planned_sessions_user_week_idx
  ON public.planned_sessions(user_id, date);

CREATE UNIQUE INDEX IF NOT EXISTS planned_sessions_tp_workout_uniq
  ON public.planned_sessions(user_id, tp_workout_id)
  WHERE tp_workout_id IS NOT NULL;

ALTER TABLE public.training_log
  ADD COLUMN IF NOT EXISTS tp_workout_id bigint,
  ADD COLUMN IF NOT EXISTS planned_session_id uuid
    REFERENCES public.planned_sessions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS max_hr integer,
  ADD COLUMN IF NOT EXISTS avg_power integer,
  ADD COLUMN IF NOT EXISTS normalized_power integer,
  ADD COLUMN IF NOT EXISTS tss numeric;

CREATE INDEX IF NOT EXISTS training_log_user_date_idx
  ON public.training_log(user_id, date);

CREATE UNIQUE INDEX IF NOT EXISTS training_log_user_tp_workout_uniq
  ON public.training_log(user_id, tp_workout_id)
  WHERE tp_workout_id IS NOT NULL;

-- De ursprungliga override-målen pekade på pensionerade tabeller.
ALTER TABLE public.coach_overrides
  DROP CONSTRAINT IF EXISTS scope_matches_target;

ALTER TABLE public.coach_overrides
  DROP COLUMN IF EXISTS week_id CASCADE,
  DROP COLUMN IF EXISTS workout_id CASCADE,
  ADD COLUMN IF NOT EXISTS week_start date,
  ADD COLUMN IF NOT EXISTS planned_session_id uuid
    REFERENCES public.planned_sessions(id) ON DELETE SET NULL;

ALTER TABLE public.coach_overrides
  ADD CONSTRAINT scope_matches_target CHECK (
    (scope = 'workout' AND planned_session_id IS NOT NULL)
    OR (scope = 'week' AND week_start IS NOT NULL)
    OR scope IN ('phase', 'volume', 'overtraining')
  );

CREATE INDEX IF NOT EXISTS coach_overrides_week_start_idx
  ON public.coach_overrides(athlete_id, week_start)
  WHERE week_start IS NOT NULL;

CREATE INDEX IF NOT EXISTS coach_overrides_planned_session_idx
  ON public.coach_overrides(planned_session_id)
  WHERE planned_session_id IS NOT NULL;
