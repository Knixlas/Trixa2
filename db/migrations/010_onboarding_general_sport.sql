-- 010_onboarding_general_sport
--
-- Onboardingen slutar anta att varje ny användare är en erfaren triatlet
-- (feedback efter första skarpa nyregistreringen, 2026-08-25).
--
-- Tre ändringar:
--
--   1. coach_name — adepten väljer vad AI-coachen heter. "Nils" var hårdkodat
--      i UI-texterna och är en persona skräddarsydd för användare 1, inte ett
--      produktnamn. NULL = neutralt "din coach" i gränssnittet.
--
--   2. races.distance vidgas. CHECK:en tillät bara triathlondistanser
--      (sprint/olympic/half/full), så en ren löpare eller cyklist kunde inte
--      registrera sitt mål alls. Engine påverkas inte: transition_days_for()
--      slår upp distansen i phase_details.post_race_recovery_days och faller
--      tillbaka på default (14 d) för värden den inte känner igen — de nya
--      distanserna får egna rader i samma yaml.
--
--   3. onboarding_version — vilken version av formuläret adepten fyllde i.
--      Låter oss fråga om det som saknas när formuläret växer, istället för
--      att skicka tillbaka alla genom hela onboardingen.
--
-- Additiv migration: inga kolumner droppas, inga rader ändras destruktivt.

ALTER TABLE public.athlete_profiles
  ADD COLUMN IF NOT EXISTS coach_name text,
  ADD COLUMN IF NOT EXISTS onboarding_version int NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.athlete_profiles.coach_name IS
  'Adeptens namn på sin AI-coach. NULL = gränssnittet säger "din coach".';
COMMENT ON COLUMN public.athlete_profiles.onboarding_version IS
  'Vilken version av onboardingformuläret som fylldes i. 0 = före versionering.';

-- Distanser: triathlon + löpning + cykel + simning + annat.
ALTER TABLE public.races
  DROP CONSTRAINT IF EXISTS races_distance_check;
ALTER TABLE public.races
  ADD CONSTRAINT races_distance_check CHECK (distance IN (
    -- triathlon
    'sprint', 'olympic', 'half', 'full',
    -- löpning
    '5k', '10k', 'half_marathon', 'marathon', 'ultra',
    -- cykel
    'gran_fondo', 'time_trial', 'stage_race',
    -- simning
    'open_water', 'swim_meet',
    -- allt annat
    'other'
  ));

COMMENT ON COLUMN public.races.distance IS
  'Tävlingsdistans. Triathlon: sprint/olympic/half/full. Löpning: 5k/10k/'
  'half_marathon/marathon/ultra. Cykel: gran_fondo/time_trial/stage_race. '
  'Simning: open_water/swim_meet. Okänd distans → other (engine använder '
  'default-återhämtning).';

-- Användare 1 behåller sin persona.
UPDATE public.athlete_profiles
SET coach_name = 'Nils'
WHERE user_id = '09db449d-b8fd-409a-b475-3401b0de9858'
  AND coach_name IS NULL;
