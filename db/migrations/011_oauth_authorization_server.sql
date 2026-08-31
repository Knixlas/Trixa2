-- 011_oauth_authorization_server
--
-- Trixa blir en OAuth 2.1-auktoriseringsserver framför /mcp, så AI-klienter
-- som inte kan sätta en egen Authorization-header (claude.ai i webb och mobil)
-- kan koppla upp sig. Personliga trixa_-tokens i api_tokens lever kvar
-- parallellt — den vägen bryts inte.
--
-- Tre tabeller:
--
--   oauth_clients              Registrerade klienter. DCR (RFC 7591) skapar
--                              dem utan inloggning; redirect_uris valideras
--                              EXAKT vid /authorize (öppen redirect annars).
--   oauth_authorization_codes  Engångskoder mellan /authorize och /token.
--                              Bara hashen lagras. code_challenge = PKCE.
--   oauth_tokens               Utfärdade access/refresh-par. Bara hashar.
--                              audience binder token till DENNA resurs, så en
--                              token utfärdad för någon annan inte går att
--                              spela upp mot oss (RFC 8707).
--
-- Ingen av tabellerna får nås av adept-JWT:n — all åtkomst sker serverside via
-- service-role. RLS på utan policies = ingen anon/authenticated-åtkomst alls.

CREATE TABLE IF NOT EXISTS public.oauth_clients (
  client_id text PRIMARY KEY,
  client_name text NOT NULL DEFAULT '',
  redirect_uris jsonb NOT NULL,
  grant_types jsonb NOT NULL DEFAULT '["authorization_code", "refresh_token"]'::jsonb,
  response_types jsonb NOT NULL DEFAULT '["code"]'::jsonb,
  -- Claudes connector är en public client med PKCE: ingen client_secret.
  token_endpoint_auth_method text NOT NULL DEFAULT 'none',
  client_uri text,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);

COMMENT ON TABLE public.oauth_clients IS
  'OAuth-klienter registrerade via DCR (RFC 7591). redirect_uris matchas exakt.';

CREATE TABLE IF NOT EXISTS public.oauth_authorization_codes (
  code_hash text PRIMARY KEY,
  client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE,
  user_id uuid NOT NULL,
  redirect_uri text NOT NULL,
  code_challenge text NOT NULL,
  code_challenge_method text NOT NULL DEFAULT 'S256',
  resource text,
  scope text NOT NULL DEFAULT '',
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS oauth_codes_expiry_idx
  ON public.oauth_authorization_codes (expires_at);

COMMENT ON TABLE public.oauth_authorization_codes IS
  'Engångskoder (~60 s). Bara sha256-hashen lagras. Återanvändning av en '
  'konsumerad kod återkallar hela token-familjen (OAuth 2.1 7.5).';

CREATE TABLE IF NOT EXISTS public.oauth_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  access_token_hash text UNIQUE NOT NULL,
  refresh_token_hash text UNIQUE,
  client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE,
  user_id uuid NOT NULL,
  audience text NOT NULL,
  scope text NOT NULL DEFAULT '',
  auth_code_hash text,
  expires_at timestamptz NOT NULL,
  refresh_expires_at timestamptz,
  revoked_at timestamptz,
  rotated_from uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);

CREATE INDEX IF NOT EXISTS oauth_tokens_access_idx
  ON public.oauth_tokens (access_token_hash);
CREATE INDEX IF NOT EXISTS oauth_tokens_refresh_idx
  ON public.oauth_tokens (refresh_token_hash);
CREATE INDEX IF NOT EXISTS oauth_tokens_user_client_idx
  ON public.oauth_tokens (user_id, client_id);

COMMENT ON TABLE public.oauth_tokens IS
  'Utfärdade access/refresh-par. Opaka slumptokens, bara hashar lagras. '
  'audience = kanonisk MCP-URI; /mcp avvisar tokens med annan audience.';
COMMENT ON COLUMN public.oauth_tokens.audience IS
  'Resursen token gäller för (RFC 8707). Skyddar mot uppspelning mot annan tjänst.';

ALTER TABLE public.oauth_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_authorization_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_tokens ENABLE ROW LEVEL SECURITY;
-- Medvetet UTAN policies: bara service-role (serversidan) rör de här raderna.
