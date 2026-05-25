-- 006_equipment_and_settings
--
-- Adept-specifik utrustning och inomhus/utomhus-preferens per disciplin.
--
-- equipment-schema (jsonb):
--   {
--     "has_trainer": true/false,         (cykel-trainer)
--     "has_treadmill": true/false,       (löpband)
--     "has_power_meter_bike": true/false,
--     "has_power_meter_run": true/false,
--     "hr_strap": true/false,
--     "pool_type": "25m"|"50m"|"open_water"|"none"
--   }
--
-- preferred_settings-schema (jsonb):
--   {
--     "swim": "any"|"indoor"|"outdoor",
--     "bike": "any"|"indoor"|"outdoor",
--     "run":  "any"|"indoor"|"outdoor"
--   }
--
-- Utrustning är vad adepten HAR. Preferred_settings är vad de VILL just nu
-- (säsongsberoende — vintertid trainer, sommar utomhus).
-- Planner kombinerar: filtrerar bort pass som kräver saknad utrustning,
-- föredrar pass som matchar setting-preferens men faller tillbaka om
-- inga matchar.

ALTER TABLE public.athlete_profiles
  ADD COLUMN IF NOT EXISTS equipment jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS preferred_settings jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.athlete_profiles.equipment IS
  '{has_trainer, has_treadmill, has_power_meter_bike, has_power_meter_run, hr_strap, pool_type}';
COMMENT ON COLUMN public.athlete_profiles.preferred_settings IS
  '{swim, bike, run}: each "any"|"indoor"|"outdoor". Säsongspreferens, inte utrustnings-krav.';

-- Seed Niklas defaults (alla utrustning på, pool 25m, alla settings any)
UPDATE public.athlete_profiles
SET equipment = '{
      "has_trainer": true,
      "has_treadmill": false,
      "has_power_meter_bike": true,
      "has_power_meter_run": false,
      "hr_strap": true,
      "pool_type": "25m"
    }'::jsonb,
    preferred_settings = '{
      "swim": "any",
      "bike": "any",
      "run": "any"
    }'::jsonb,
    updated_at = now()
WHERE user_id = '09db449d-b8fd-409a-b475-3401b0de9858';
