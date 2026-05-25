-- 004_seed_niklas
--
-- Skapar athlete_profiles-rad för Niklas baserat på:
--   - public.profiles (källa: e-post, basic-info, race-info)
--   - coach/data/athlete_config.yaml (källa: aktuella tröskelvärden)
--   - Niklas medicinska kontext från CLAUDE.md / health_notes
--
-- DDL-migrationen 001-003 måste ha körts innan denna.
-- Är idempotent via ON CONFLICT — kan köras om för att uppdatera fält.

INSERT INTO public.athlete_profiles (
  user_id,
  goal,
  sports,
  experience_level,
  weekly_hours,
  weekly_days,
  race_type,
  race_date,
  time_goal,
  ftp,
  lthr,
  swim_css,
  run_threshold_pace,
  preferred_language,
  coach_tone,
  show_advanced_metrics,
  garmin_athlete_id,
  health_conditions,
  active_concerns,
  medications,
  phase_state,
  notes
) VALUES (
  '09db449d-b8fd-409a-b475-3401b0de9858',  -- Niklas auth.users.id
  'ironman',
  ARRAY['swim', 'bike', 'run'],
  'advanced',
  12.0,
  7,
  'ironman',
  '2026-08-15',
  'sub-13:00 (1:30 sim / 6:30 cykel / 4:30 löp)',
  198,    -- FTP-watts från athlete_config.yaml 2026-05-23 (estimat)
  170,    -- AT-puls löp från athlete_config; LTHR-bike sätts via phase-state om relevant
  '2:15', -- CSS från athlete_config 135s/100m
  '5:15', -- threshold-pace löp från athlete_config 315s/km
  'sv',
  'neutral',
  true,
  '98057fa1-4fb9-48f5-be86-b31272dcfed0',  -- garmin_coach.athlete_profile.id
  '[
    {
      "name": "Hashimoto/hypotyreos",
      "diagnosed_year": 2018,
      "medication": "Levaxin + Liothyronine",
      "dose": "Levaxin 150 µg + Liothyronine 10 µg",
      "notes": "Påverkar återhämtningskinetik. Stabilt med medicinering."
    },
    {
      "name": "Ozempic-behandling",
      "diagnosed_year": 2025,
      "medication": "Ozempic",
      "dose": "1,5 mg",
      "notes": "Viktnedgång från 107 kg. Påverkar nutrition under långpass — hård gräns för intag."
    },
    {
      "name": "Pollenallergi",
      "diagnosed_year": null,
      "medication": "Aerus",
      "dose": null,
      "notes": null
    }
  ]'::jsonb,
  '[
    {
      "name": "Ryggskott",
      "severity": 2,
      "since_date": "2026-04-28",
      "needs_followup": false,
      "follow_up_by": null,
      "notes": "Rehab pågår med \"21 day back pain relief\"-program. Smärta sjunkit från 5-6/10 till 2-3/10. Förbättring kontinuerlig."
    },
    {
      "name": "Deltamuskelsmärta + uppmätt styrkebortfall",
      "severity": 3,
      "since_date": null,
      "needs_followup": true,
      "follow_up_by": "fysio",
      "notes": "Etiologi oklar. Skyddsräcke: kräver fysio-undersökning om symtomet inte vänder inom 1-2 veckor. Påverkar styrkedelen — använder 5 kg shoulder press just nu."
    },
    {
      "name": "Vänster biceps",
      "severity": 1,
      "since_date": null,
      "needs_followup": false,
      "follow_up_by": null,
      "notes": "Förbättras."
    },
    {
      "name": "Stress-/utmattningsperiod",
      "severity": 2,
      "since_date": "2026-01-01",
      "needs_followup": false,
      "follow_up_by": null,
      "notes": "Akut 4-5 mån från intensivt produktarbete. Börjar klinga av."
    }
  ]'::jsonb,
  '[
    {"name": "Levaxin", "dose": "150 µg", "since_date": null, "prescribed_for": "Hashimoto", "notes": null},
    {"name": "Liothyronine", "dose": "10 µg", "since_date": null, "prescribed_for": "Hashimoto", "notes": null},
    {"name": "Ozempic", "dose": "1,5 mg", "since_date": "2025-05-01", "prescribed_for": "Viktnedgång", "notes": "Påverkar nutrition under långpass"},
    {"name": "Aerus", "dose": null, "since_date": null, "prescribed_for": "Pollenallergi", "notes": null}
  ]'::jsonb,
  '{}'::jsonb,  -- phase_state — Trixa-planner sätter detta vid första körning
  'Migrerad från public.profiles 2026-05-25. Tröskelvärden från athlete_config.yaml (estimat 2026-05-23). Validera FTP, CSS, threshold-pace mot riktigt test när möjligt.'
)
ON CONFLICT (user_id) DO UPDATE SET
  goal = EXCLUDED.goal,
  sports = EXCLUDED.sports,
  experience_level = EXCLUDED.experience_level,
  weekly_hours = EXCLUDED.weekly_hours,
  weekly_days = EXCLUDED.weekly_days,
  race_type = EXCLUDED.race_type,
  race_date = EXCLUDED.race_date,
  time_goal = EXCLUDED.time_goal,
  ftp = EXCLUDED.ftp,
  lthr = EXCLUDED.lthr,
  swim_css = EXCLUDED.swim_css,
  run_threshold_pace = EXCLUDED.run_threshold_pace,
  garmin_athlete_id = EXCLUDED.garmin_athlete_id,
  health_conditions = EXCLUDED.health_conditions,
  active_concerns = EXCLUDED.active_concerns,
  medications = EXCLUDED.medications,
  notes = EXCLUDED.notes,
  updated_at = now();
