-- 013_exercise_log_progression
--
-- Styrkeloggen tog emot vikt och ansträngning men ingenting läste dem
-- tillbaka. Adepten fick minnas förra passets vikt själv och gissa nästa,
-- trots att Trixa hade svaret i tabellen. Passbanken beskrev redan modellen
-- ("progredierar nästa gång samma RIR nås vid lägre ansträngning — detta ÄR
-- autoreglering", strength_MS.yaml); den saknade bara en väg tillbaka in i
-- planeringen.
--
-- Två additiva ändringar som gör progressionsuppslaget möjligt och robust:
--
--   exercise_code  Loggen band bara övningsnamnet. Skrivs katalogens namn om
--                  ("Knäböj" → "Knäböj med skivstång") tappar historiken
--                  kontakten med övningen och progressionen börjar om från
--                  noll. Koden är stabil; namnet är en etikett.
--
--   index          Uppslaget är "senaste loggen för den här övningen" en gång
--                  per övning i passet. Utan index blir det en seq scan per
--                  rad i loggformuläret.
--
-- Inga rader ändras, inga kolumner tas bort. Befintliga loggar har NULL i
-- exercise_code och matchas fortsatt på namn.

alter table public.exercise_logs
  add column if not exists exercise_code text;

comment on column public.exercise_logs.exercise_code is
  'Passbankens övningskod (strength_exercises.yaml). Stabil nyckel för '
  'progressionshistorik; exercise_name är etiketten adepten ser.';

create index if not exists exercise_logs_progression_idx
  on public.exercise_logs (user_id, exercise_name, session_date desc);

create index if not exists exercise_logs_code_idx
  on public.exercise_logs (user_id, exercise_code, session_date desc)
  where exercise_code is not null;
