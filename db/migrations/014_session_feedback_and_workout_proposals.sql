-- 014_session_feedback_and_workout_proposals
--
-- Två nya tabeller. Inga befintliga tabeller, kolumner eller rader ändras.
--
-- session_feedback
--   Trixa visste om ett pass blev gjort och hur jobbigt det var (rpe), men
--   inte om adepten ville göra det igen. Ett hårt pass kan vara omtyckt och
--   ett lätt pass tråkigt; ansträngning säger inte vilket. Loggformuläret
--   frågar nu "Vill du göra passet igen?" (ja / kanske / nej). En rad per
--   (adept, planerat pass); ett nytt svar ersätter det gamla och updated_at
--   visar när. workout_code sparas för sig så att svaren går att räkna per
--   passbankspass även om den planerade raden senare tas bort.
--
-- workout_proposals
--   AI-coacher (MCP) får föreslå nya pass till passbanken, helst sådana
--   adepten gillat. Förslagen går INTE in i passbanken direkt: de granskas
--   (status pending → accepted/rejected) och ett godkänt förslag skrivs in i
--   passbankens YAML av import-skriptet (status imported), så att det går
--   genom validator, språkkontroll och tester innan någon adept får det.
--   evidence räknas av servern ur loggen och session_feedback — inte ur
--   coachens påstående. Rader tas aldrig bort; granskningen sparas med vem
--   och när.

create table if not exists public.session_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  planned_session_id uuid references public.planned_sessions(id) on delete set null,
  workout_code text,
  session_date date,
  again text not null check (again in ('yes', 'maybe', 'no')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, planned_session_id)
);

create index if not exists session_feedback_user_idx
  on public.session_feedback (user_id, session_date desc);
create index if not exists session_feedback_code_idx
  on public.session_feedback (workout_code) where workout_code is not null;

alter table public.session_feedback enable row level security;

create policy "Adepten hanterar sina egna passkänslor"
  on public.session_feedback for all
  using (user_id = auth.uid());

comment on table public.session_feedback is
  'Adeptens svar på "Vill du göra passet igen?" per planerat pass (yes/maybe/no).';

create table if not exists public.workout_proposals (
  id uuid primary key default gen_random_uuid(),
  athlete_user_id uuid not null references public.profiles(id) on delete cascade,
  proposed_via_token_id uuid,
  proposer_name text,
  sport text not null,
  category text not null,
  name text not null,
  workout jsonb not null,
  rationale text not null default '',
  source_session_ids jsonb not null default '[]'::jsonb,
  evidence jsonb not null default '{}'::jsonb,
  status text not null default 'pending'
    check (status in ('pending', 'accepted', 'rejected', 'imported')),
  review_note text,
  reviewed_by uuid references public.profiles(id),
  reviewed_at timestamptz,
  imported_code text,
  created_at timestamptz not null default now()
);

create index if not exists workout_proposals_status_idx
  on public.workout_proposals (status, created_at desc);

-- Bara servern (service-role) läser och skriver förslagen.
alter table public.workout_proposals enable row level security;

comment on table public.workout_proposals is
  'AI-coachers förslag till passbanken. Granskas; godkända importeras till YAML.';
