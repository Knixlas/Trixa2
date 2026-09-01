-- 012_obstacle_race_distances
--
-- Hinderbanelopp saknade distanstyp (feedback från skarp användning
-- 2026-09-01). races.distance täckte triathlon-, löp-, cykel- och
-- simdistanser, så en OCR-adept fick välja "10 km" och beskriva det verkliga
-- loppet i time_goal som fritext — som ingen logik läser.
--
-- Skillnaden är inte kosmetisk. Ett hinderlopp kräver grepp, drag och
-- klättring; ett tiokilometerslopp kräver löpning. Motorn planerade för fel
-- sak, och post_race_recovery_days låg fel: hinderlopp bryter ner mer än en
-- löprunda på samma sträcka, eftersom överkroppen får excentrisk belastning
-- den inte får av löpning.
--
-- Tre distanser, samma logik som löpningens uppdelning:
--   obstacle_sprint   — upp till ~7 km (Tough Viking Sprint m.fl.)
--   obstacle_standard — ~8-15 km (klassiska formatet)
--   obstacle_ultra    — längre/uthållighets-OCR (varv- och timformat)
--
-- Additiv migration: bara CHECK:en vidgas, inga rader ändras.

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
    -- hinderbana
    'obstacle_sprint', 'obstacle_standard', 'obstacle_ultra',
    -- allt annat
    'other'
  ));

COMMENT ON COLUMN public.races.distance IS
  'Tävlingsdistans. Triathlon: sprint/olympic/half/full. Löpning: 5k/10k/'
  'half_marathon/marathon/ultra. Cykel: gran_fondo/time_trial/stage_race. '
  'Simning: open_water/swim_meet. Hinderbana: obstacle_sprint/'
  'obstacle_standard/obstacle_ultra. Okänd distans → other (engine använder '
  'default-återhämtning).';
