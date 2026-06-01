-- 001_athlete_profiles_structured_fields
--
-- Lägger till strukturerade hälso-, tillstånds- och fas-fält på den nya
-- athlete_profiles-tabellen. Det här är Trixas truth-källa för adept-state.
-- Den gamla public.profiles behålls för auth + grundinfo (e-post, namn)
-- men Trixa läser och skriver strukturerat hit istället.
--
-- Fältdesign:
--
--   injuries           — lista av {location, severity (1-5), since_date,
--                        affects_disciplines (lista swim/bike/run/strength),
--                        status (active/healing/healed), notes}
--   health_conditions  — lista av kroniska tillstånd:
--                        {name, diagnosed_year, medication, dose, notes}
--   active_concerns    — akuta bekymmer som ej är fullskaliga skador:
--                        {name, severity (1-5), since_date,
--                         needs_followup, follow_up_by, notes}
--   medications        — lista av {name, dose, since_date,
--                        prescribed_for, notes}
--   garmin_athlete_id  — länkar till garmin_coach.athlete_profile.id
--   phase_state        — {current_phase, period, weeks_in_phase,
--                        last_transition_at}

ALTER TABLE public.athlete_profiles
  ADD COLUMN IF NOT EXISTS injuries jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS health_conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS active_concerns jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS medications jsonb NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS garmin_athlete_id uuid,
  ADD COLUMN IF NOT EXISTS phase_state jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS notes text DEFAULT '';

CREATE INDEX IF NOT EXISTS athlete_profiles_garmin_athlete_id_idx
  ON public.athlete_profiles(garmin_athlete_id);

COMMENT ON COLUMN public.athlete_profiles.injuries IS
  'Lista av {location, severity (1-5), since_date, affects_disciplines, status, notes}.';
COMMENT ON COLUMN public.athlete_profiles.health_conditions IS
  'Kroniska tillstånd: {name, diagnosed_year, medication, dose, notes}.';
COMMENT ON COLUMN public.athlete_profiles.active_concerns IS
  'Akuta bekymmer: {name, severity, since_date, needs_followup, follow_up_by, notes}.';
COMMENT ON COLUMN public.athlete_profiles.medications IS
  'Lista av {name, dose, since_date, prescribed_for, notes}.';
COMMENT ON COLUMN public.athlete_profiles.garmin_athlete_id IS
  'Länkar till garmin_coach.athlete_profile.id för Garmin-sync.';
COMMENT ON COLUMN public.athlete_profiles.phase_state IS
  '{current_phase, period, weeks_in_phase, last_transition_at}. Uppdateras av Trixa-planner.';
