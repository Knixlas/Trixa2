# 10 — Self-service credentials (Strava, TP, Claude)

**Status 2026-06-15.** Mål: varje adept lägger in *sina egna* uppkopplingar i
Trixa-UI:t och får dem att fungera, i stället för admin-konfig/env per användare.

Princip: credentials bor som **per-user-rader i Supabase** (RLS-skyddade), inte
i global env. Infra-hemligheter (OAuth-app-secret, state-secret, service-role-
nyckel) stannar i env — de är inte per-user.

---

## Strava — klart (per användare)

- OAuth-tokens ligger redan per `user_id` i `public.strava_tokens`. "Anslut
  Strava" kör hela OAuth-flödet och sparar adeptens tokens.
- Det enda globala är Trixas Strava-**app** (`STRAVA_CLIENT_ID/SECRET` i env) —
  en API-app för hela Trixa, korrekt OAuth-modell.
- **RLS härdad 2026-06-15** (`harden_strava_tokens_rls`):
  - SELECT var redan `auth.uid() = user_id` → ingen läser annans tokens.
  - INSERT var `public/true` (vem som helst kunde skriva rad för valfri
    user_id) → nu `authenticated` med `with check (auth.uid() = user_id)`.
  - UPDATE fick `with check`; DELETE-policy tillagd (egen rad).
  - Backend skriver via service-role → kringgår RLS, opåverkat.

## TrainingPeaks — login-form byggd (2026-06-15)

- Cookie (`Production_tpAuth`) lagras per `user_id` i `public.tp_auth` (RLS:
  `user_id = auth.uid()`, ALL). Lagringen var redan multi-tenant.
- **Gapet stängt:** tidigare kunde cookien bara läggas in via CLI. Nu finns en
  **headless login-form** i Inställningar:
  - `coach/integrations/trainingpeaks/login.py` — `login_and_get_cookie()`:
    GET login-sidan → extrahera `__RequestVerificationToken` → POST
    användarnamn/lösen i samma session → fånga `Production_tpAuth` ur jaren.
  - `login_and_store()` → `auth_store.store_cookie()` (per user_id).
  - UI: `POST /ui/tp/login`, form `_tp_login_form.html` (kopplat → "rotera"
    under details; ej kopplat → primär login).
  - **Lösenordet sparas aldrig** — bara den resulterande sessionen.
- **Skörhet (medveten):** TP har ingen publik OAuth. Slår TP på CAPTCHA/MFA
  eller ändrar login-sidan slutar det fungera → `login.py` ger uttryckliga fel
  (`loginfail`/`loginerror`-flash) och man faller tillbaka på manuell
  cookie-capture (`auth_store` CLI). Tester: `coach/tests/test_tp_login.py`
  (mockad session: happy path, fel uppgifter, captcha, ändrad sida, kort cookie).
- **Att verifiera live:** flödet är inte kört mot skarp TP härifrån (sandbox når
  inte tp). Kör en riktig login efter deploy; justera fältnamn/URL om TP avviker.

## Claude (Nils) — BYO-key: DESIGN, ej byggt

Idag finns **ingen** Anthropic-nyckel eller LLM-kod i Trixa (per arkitektur:
"Inga LLM-anrop i Trixa-kod"). Nils kör i ett eget Claude-skikt utanför. Att
låta adepter lägga in egen Claude-nyckel är därför **ny yta**, inte en flytt.

### Frågan att svara först (produkt)
Vem betalar Claude-anropen, och var kör Nils?
- **A. BYO-key:** adepten lägger in sin egen Anthropic-nyckel; Trixa anropar
  Claude åt hen. Adepten bär kostnaden. Kräver att Nils-anropet byggs *in* i en
  tjänst (bryter dagens "ingen LLM i Trixa-kod" — gör det i ett separat
  `nils/`-skikt, inte i engine/planner).
- **B. Server-nyckel + debitering:** Trixa har en nyckel, Nils är en betald
  add-on (matchar CLAUDE.md: "add-on inom Max-abbo"). Ingen per-user-nyckel.
- **C. Status quo:** Nils kör i Claude-projekt-tråd, ingen nyckel i Trixa.

Rekommendation: **B** ligger närmast affärsmodellen; **A** bara om adepter
uttryckligen vill köra på egen nyckel/eget konto.

### Om BYO-key (A) väljs — skiss
- Tabell `public.llm_credentials`: `user_id uuid unique`, `provider text`
  (default `anthropic`), `api_key_encrypted text`, `updated_at`. RLS som
  `tp_auth` (`user_id = auth.uid()`, ALL).
- **Kryptering i vila:** nyckeln är en långlivad hemlighet — kryptera med
  pgcrypto/Vault, lagra inte i klartext. (TP-cookie/Strava-token är kortlivade;
  en API-nyckel är värre att läcka.) Läs/dekryptera bara i backend (service-role).
- Inmatning: fält i Inställningar (samma mönster som TP-login), validera mot ett
  billigt Anthropic-anrop (t.ex. token-count) innan vi sparar.
- Nils-skikt: nytt `nils/` (LLM, fritext) som läser samma engine-output men
  ringer Claude med användarens nyckel. Modell: senaste Opus (Nils kräver Opus
  enligt CLAUDE.md). Aldrig i `coach/engine`/`coach/trixa` (deterministiskt).

### Säkerhet (gäller alla self-service-credentials)
RLS per user_id; läs/skriv bara via service-role i backend/worker; exponera
aldrig i klienten; kryptera långlivade hemligheter i vila. Infra-secrets
(`STRAVA_*`, `SUPABASE_SERVICE_ROLE_KEY`) är inte per-user och stannar i env.
