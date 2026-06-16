-- 009_integration_runs
--
-- Beständig TP-auth och driftstatus för externa integrationer.
-- Service-role läser/skriver; inga klientpolicies skapas.

CREATE TABLE IF NOT EXISTS public.tp_auth (
  user_id uuid PRIMARY KEY REFERENCES public.profiles(id) ON DELETE CASCADE,
  cookie text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.tp_auth ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.tp_auth IS
  'TrainingPeaks-cookie per användare. Endast backend med service-role får läsa.';

CREATE TABLE IF NOT EXISTS public.integration_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES public.profiles(id) ON DELETE CASCADE,
  integration text NOT NULL,
  operation text NOT NULL,
  status text NOT NULL CHECK (status IN ('running', 'success', 'failed')),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  records_processed integer NOT NULL DEFAULT 0,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS integration_runs_latest_idx
  ON public.integration_runs(integration, operation, finished_at DESC);

CREATE INDEX IF NOT EXISTS integration_runs_user_latest_idx
  ON public.integration_runs(user_id, integration, finished_at DESC);

ALTER TABLE public.integration_runs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.integration_runs IS
  'Beständig körhistorik för TP-synk, planner och andra externa integrationer.';
