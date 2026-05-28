-- 007_garmin_oauth_tokens
--
-- Garmin OAuth-tokens i Supabase istället för i fil/GitHub Secret.
--
-- Bakgrund: Garmin använder single-use refresh tokens. Varje sync får
-- ett nytt refresh_token som måste sparas tillbaka. Med tokens i
-- GitHub Secret krävs manuell rotation varje gång → 17/22 failures
-- senaste 14 dagar pga inaktuella tokens.
--
-- Genom att lagra tokens i Supabase kan workflow:n läsa OCH skriva
-- tokens med samma SUPABASE_SERVICE_ROLE_KEY den redan har. Inga
-- GitHub Secret-uppdateringar krävs.
--
-- email som PK eftersom det är vad GARMIN_EMAIL i env är — vi vet
-- redan det innan vi gjort vårt första API-anrop, ingen cirkulär
-- "behöver user_id för att hämta tokens som behövs för user_id"-loop.

CREATE TABLE IF NOT EXISTS garmin_coach.oauth_tokens (
  email text PRIMARY KEY,
  di_token text NOT NULL,
  di_refresh_token text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE garmin_coach.oauth_tokens IS
  'Garmin OAuth tokens. Single-use refresh tokens roteras vid varje sync; skrivs tillbaka hit för att eliminera manuell rotation.';
