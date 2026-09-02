# Trixa2 — Claude-kontext

Denna fil läses först i varje ny Claude-tråd som arbetar med Trixa2.
Den ska hålla en ny session uppdaterad utan att behöva återupprepa förra trådens upptäckter.

## Två produkter, en kärna

**Trixa** och **Nils** är två separata produkter som delar samma kodbas men har olika ekonomi och tekniska krav. Att blanda ihop dem är den vanligaste arkitekturmissen i det här projektet.

| | Trixa | Nils |
|---|---|---|
| Vad | Publik tränare | Personlig tränare |
| Affärsmodell | SaaS för triathleter | Add-on inom Max-abbo |
| Teknik | Ren kod + databas, **ingen LLM** | LLM (Opus) ovanpå Trixas kod |
| Input från adept | Formulär, krysslistor, ratings | Fritext, samtal |
| Output till adept | Färdiga protokoll | Tolkning, anpassning, mänsklig respons |
| Skalbarhet | Tusentals adepter | En adept per Max-abbo |
| Status | Engine + adapter klara, protokoll saknas | Fungerar i Claude-projekt-tråd |

### Konsekvenser

- **Trixa måste klara sig själv.** Om Trixa-koden anropar en LLM för något steg så är produkten fel byggd. Engine, adapter, passbank, .fit-export, veckoplangenerator, rapportprotokoll — allt körs utan LLM.
- **Nils är ett tunt lager ovanpå Trixa.** Hen läser samma engine-output, men kan tolka fritext och formulera nyans. Hens värde är coach-beslut med kontext (manual_override), inte att vara plan-genereringsmotorn.
- **Krasst:** Nils kräver Opus. Sonnet/Haiku duger inte för den nyans som krävs.
- **Personligheter är konfigurerbara.** Nils är skräddarsydd för Niklas. På sikt kan det finnas Maja (hårdare), Anders (teknik-fokuserad) etc. som delar Trixas motor men har olika personlighetsfiler.
- **Nils som medskapare av pass:** LLM:n bidrar mest värde i att resonera fram nya pass utifrån kontext. Flödet är Nils-förslag-i-fritext → formalisering till YAML → passbanken växer → Trixa använder.

## Arkitektoniska lager

```
                    ┌── Trixa (kod, formulär, protokoll) ──→ Adept
Engine + Adapter ───┤
   + Passbank       └── Nils (LLM, fritext, tolkning) ─────→ Adept
```

- **Engine** (`coach/engine/`): Bestämmer fas, kategori (AE/ME/AC/...), volym, tak.
- **Adapter** (`coach/trixa/planner.py::_build_athlete_state` / `_build_ot_signals`): Hämtar Supabase-data, bygger engine-inputs. (`coach/adapters/` och `coach/engine/garmin.py` finns inte längre — den senare var död kod, borttagen 2026-09-02.)
- **Passbank** (`coach/data/workouts/` — *ej byggd än*): Konkreta pass per kategori. Renderar mot adeptens zoner till människoläsbar text + `.fit`-fil.
- **Trixa-skikt** (*ej byggt än*): Formulär-input, protokoll-output, veckoplan-generator.
- **Nils-skikt** (`coach/personas/` — *ej formaliserat än*): LLM-personlighet, manual_override-beslut.

## Adept och mål

- **Adept:** Niklas Svidén, `profiles.id = 09db449d-b8fd-409a-b475-3401b0de9858`, role=athlete
- **Garmin-id:** `garmin_coach.athlete_profile.id = 98057fa1-4fb9-48f5-be86-b31272dcfed0`, garmin_user_id `70747`
- **Nästa tävling:** Ironman Kalmar, 2026-08-15
- **Coach:** Coach Svidén (`profiles.id = 4e225307-ee66-4bf8-a141-69f52218e2ce`), role=coach
- **Historik:** 13 IM-finishes, gjort tighta upprampningar förut

### Medicinsk kontext (delad i tråd 23 maj 2026)
- Hashimoto/hypotyreos sedan 2018, medicinering Levaxin + Liothyronine
- Ozempic ~1 år (viktnedgång från 107 kg), gör nutrition under långpass till hård gräns
- Akut stress-/utmattningsperiod 4-5 mån, börjar klinga av
- Aktiva problem: ryggrehab (gott resultat), **deltamuskelsmärta + uppmätt styrkebortfall** (etiologi oklar)

Detaljerade hälso-konversationer hör hemma i planeringstrådar med Nils, inte i meta/arkitekturtrådar. Skyddsräcke: delta-symtomet kräver fysio-undersökning om det inte vänder inom 1-2 veckor.

## Databas

- **Supabase-projekt:** `Trixa` (utan 2). project_id `vtwqebihrxrufgrzmefe`, eu-west-1.

### Schema-uppdelning

| Schema | Roll |
|---|---|
| `public` | Profiler, träningsloggning, coach-output, app-tabeller |
| `garmin_coach` | **Primär datakälla** — Garmin-synkad aktivitet och daily metrics |

### Viktiga tabeller

**`garmin_coach.activities`** (4185 rader, sedan 2006-08-03): `duration_sec`, `hr_zones_time` (jsonb!), `training_effect_*`, `training_load`, `normalized_power`.

**`garmin_coach.daily_metrics`** (30 rader, sedan 2026-04-24):
- `resting_hr` — just nu NULL (hål i sync)
- `hrv_last_night_ms` vs `hrv_baseline_low/high`
- `sleep_score` (**OBS:** klockan tappar timmar i början av natten)
- `readiness_score`, `body_battery_*`, `stress_avg`
- `acute_load`, `chronic_load`, `load_ratio` — just nu NULL

**`garmin_coach.athlete_profile`** (1 rad): Testvärden, zoner som jsonb. **TODO**: `user_id` är NULL — bör länkas till `public.profiles.id`.

**`public.profiles`**: `ftp`, `at_hr`, `css`, tävlingsmål, självskattningar, `injuries`-fritext, `health_notes`-fritext.

### ⚠️ MASTER-tabeller sedan 2026-06-07 (docs/08) — LÄS HIT FÖRST

All plan- och utfört-data bor i **två master-tabeller**. Nils, Trixa2 och mobil-Claude läser/skriver SAMMA tabeller:

| Roll | Tabell | Nyckel | Not |
|---|---|---|---|
| **Planerade pass** | `public.planned_sessions` | `user_id` (= profiles.id) | `origin` skiljer skapare: `'nils'` / `'trixa2'` / `'manual'` / NULL (legacy). Sport på svenska: Cykel/Sim/Löpning/Styrka/Vila |
| **Utförda pass** | `public.training_log` | `user_id` | källtaggad (`source`: strava/tp/chat/manual), `planned_session_id` länkar mot plan |

**PENSIONERADE (DROPPADE) tabeller — finns INTE längre:** `training_weeks`, `workouts`, `training_plans` (migration `retire_redundant_plan_tables`, 2026-06-07). Gamla instruktioner som pekar dit ger tomma svar/fel — fråga `planned_sessions`/`training_log` istället.

**Nils skriver plan:** direkt i `planned_sessions` med `origin='nils'` — via MCP `plan_session` eller `/agent/plan/session`. (`garmin_coach.planned_workouts` speglas INTE längre in; skriv aldrig dit.) **Nils vinner alltid:** motorn genererar aldrig pass för dagar som redan har mänskligt skapade rader.

**Overrides:** `coach_overrides.athlete_id` pekar på **`athlete_profiles.id`** (Niklas: `81b667bc-f37c-4311-a45e-1b0a28d1ada7`), INTE user_id.

Övriga app-tabeller: `coach_alerts`, `weekly_reports`, `exercise_logs`, `personal_records`, `chat_messages`.

## Datainsamling — Garmin→Supabase

> **⚠️ Arkitektur under omläggning (2026-06-07): TrainingPeaks blir enda integration.**
> Garmins kortlivade MFA-tokens gör direktsynken ohållbar. Ny modell: Garmin-klockan
> AutoSyncar till TrainingPeaks (aktiviteter + HRV/sömn/RHR/Body Battery), Trixa läser
> **bara TP** och skriver strukturerade pass tillbaka via TP→Garmin AutoSync. Koden ligger
> i `coach/integrations/trainingpeaks/` (client/mapping/structure/sync/workout_writer,
> 12 tester gröna). `garmin_coach.activities`/`daily_metrics` blir en intern cache som
> fylls från TP — engine/adapter rörs inte. **Design:** `docs/06_TP_INTEGRATION_REBUILD.md`.
> **Drift/go-live:** `docs/07_TP_SYNC_RUNBOOK.md`. Vinst: TP-cookien lever veckor (ej dagar),
> ingen MFA/TTY; och TP:s CTL/ATL/TSB fyller `load_ratio` som Garmin-synken lämnade NULL.
> Garmin-cronen nedan pensioneras vid go-live (behålls för rollback).

Sync-pipelinen lever i ett separat GitHub-repo: **`Knixlas/Trixa2`** (publikt).

**Detaljerad runbook & troubleshooting:** se `02_GARMIN_SYNC_RUNBOOK.md` (uppladdad i projektkunskap). Vid sync-problem — **kolla alltid `garmin_coach.sync_log` först, inte `activities`/`daily_metrics`**. De senare uppdateras bara när det finns ny data och säger ingenting om huruvida synken funkar.

**Schemalagd körning:**
- Cron `30 5 * * *` (05:30 UTC dagligen, ≈07:30 svensk sommartid)
- Workflow: `.github/workflows/sync.yml`
- Kör `python sync.py activities --limit 10` följt av `python sync.py daily --from yesterday --to today`

**Manuell trigger:**
- URL: https://github.com/Knixlas/Trixa2/actions/workflows/sync.yml
- Klicka "Run workflow" → välj `sync_type` (`daily`, `activities`, `profile`, `full`)
- Parametrar: `days` (default 1) och `activities_limit` (default 20)
- **iOS Shortcut "Synka Trixa"** finns på hemskärmen — POST mot dispatches-endpoint med `sync_type=full`. Ett tryck triggar full sync. PAT lagrad i Authorization-headern. Vid token-rotation: uppdatera headern.

**Sync-typer:**
- `daily`: hämtar daily_metrics för datumintervall (sömn, HRV, RHR, etc.)
- `activities`: hämtar senaste N aktiviteter
- `profile`: uppdaterar athlete_profile
- `full`: alla tre kombinerat — **default-val för manuell trigger**

**Hälsoövervakning:**
- `.github/workflows/token-health.yml` körs 06:30 UTC dagligen
- Failar om senaste lyckad sync >26h gammal → GitHub mejlar repo-ägaren
- Varnar (utan att faila) om >18h gammal

**Token-rotation (när Garmin invaliderar refresh-token):**
- Symptom i `sync_log.error_message`: "Cachade tokens funkar inte och vi ar i CI utan TTY"
- Åtgärd: kör `.\garmin-mcp\scripts\refresh_garmin_tokens.ps1` lokalt från Trixa2-roten
- Tid: ~2 min, varav ~30 sek MFA-väntan (mobilkod/SMS)
- Kan inte automatiseras bort — Garmins login kräver MFA, GitHub Actions har ingen TTY

**Verifiering:** Status loggas i `garmin_coach.sync_log` (kolumner: `sync_type`, `status`, `started_at`, `records_synced`, `error_message`, `metadata`).

**Klassisk bugg:** `gh secret set --body $value` i PowerShell strippar citationstecken ur JSON-värden. Använd alltid stdin-pipe: `$value | gh secret set NAME --repo $Repo`. Se runbook + `01_PATCH_setup_github_secrets.md` för detaljer.

## Repo-struktur

```
Trixa2/
├── CLAUDE.md
├── coach/
│   ├── data/                   ← GENERELL träningsfilosofi (inga adept-värden!)
│   │   ├── athlete_config.example.yaml  ← dev-fixture, generiska värden
│   │   ├── phases.yaml
│   │   ├── phase_details.yaml  ← inkl. recovery_week + per-period max-pass
│   │   ├── workouts.yaml       ← passtyp-koder (AE/ME/AC/...)
│   │   ├── strength.yaml       ← protocol_parameters per protokoll
│   │   ├── nutrition.yaml      ← generella defaults (per-adept i DB)
│   │   ├── overtraining.yaml
│   │   ├── alerts.yaml
│   │   ├── session_mapping.yaml
│   │   └── workouts/           ← passbank (124 pass + 27 drills, inkl. brick)
│   ├── engine/
│   │   ├── zones.py
│   │   ├── phases.py
│   │   ├── workouts.py
│   │   ├── strength.py
│   │   ├── overtraining.py
│   │   └── profile.py          ← profile_from_athlete_row = enda parsningsvägen
│   ├── adapters/
│   │   └── garmin.py
│   ├── integrations/trainingpeaks/
│   ├── personas/
│   │   └── nils.yml               ← Nils-persona (uppladdad i projektkunskap)
│   └── trixa/                  ← planner, cron, races, alerts, db
├── trixa_api/                  ← FastAPI + /ui (login, signup, onboarding, settings)
│   ├── agent_api.py            ← /agent/* REST, per-adept Bearer-token
│   ├── mcp_server.py           ← /mcp MCP-server (samma token, MCP-protokoll)
│   └── oauth_server.py         ← OAuth 2.1 AS: .well-known + /oauth/* (claude.ai)
├── db/migrations/              ← 001-011 (010 coach_name + grendistanser, 011 oauth)
└── coach/tests/
```

**Adept-data bor i Supabase, INTE i coach/data.** `public.athlete_profiles`
(en rad per användare) bär trösklar (ftp/lthr/lthr_bike/max_hr/resting_hr/
swim_css/run_threshold_pace), threshold_meta (källa+testdatum), veckoschema,
recovery_week_ratio ('3:1'|'2:1'), nutrition-överridor, hälso-jsonb och
onboarded_at. `public.races` (athlete_id → athlete_profiles.id) är
tävlingskalendern — planner läser nästa A-race via `coach/trixa/races.py`
med fallback till athlete_profiles.race_date. `races.yaml` och
`athlete_config.yaml` är BORTTAGNA (2026-07-02).

## Passbank — design (ej byggd än)

Designval landade i tråd 23 maj 2026:

- **Kvalitetspass (AC, ME, MF, TE)**: konkreta YAML-pass, 3-5 per kategori, Nils/Trixa väljer slumpvis
- **Volympass (AE)**: parametriserade mallar med `duration_min` som flex-parameter
- **Brick**: börja konkret, parametrisera om mönster framträder

Varje pass har: `code`, `discipline`, `category`, `phase_appropriate`, `intent` (prosa-syfte), `main_set` (strukturerad data), zoner som *referenser* (renderaren slår upp), **cykel: både puls och watt**, `total_duration_min` med `flexible_range`, `abort_conditions`.

Två outputs per pass:
1. Människoläsbar prosa
2. `.fit`-fil för Garmin Connect

**Bygg inkrementellt**: 3-5 pass per disciplin för aktuell fas, lägg till efter behov. **Bygg i separat tråd** från veckoplanering.

## Trixa-protokoll — design (ej startad)

Trixa är just nu en motor utan kanal till adepten. Hen kommunicerar bara via Nils. Det är en produkt-gap som behöver designas medvetet.

Trixa-protokollet ska definiera:
- **Inputformulär**: veckorapport, mående-check, testvärden, symtom-rapportering, träningslogg-bekräftelse
- **Outputprotokoll**: veckans plan, varningar, justeringar, frågor till adept
- **Datamodell**: strukturerade injury-fält (`has_active_injury: bool`, `injury_locations: list[enum]`, `injury_severity: int`), så Trixa kan läsa dem deterministiskt. Fritextfälten i `profiles.injuries` blir kvar för Nils, inte för Trixa.

Design-arbete, inte kodarbete. 1-2 trådar när det är dags.

## Coach-praxis: manual_override

Engine ger deterministiska rekommendationer. Coachen (Nils) kan ha kontext engine inte ser — medicinskt, säsong, adept-önskemål. Då åsidosätts engine.

Spårbarhetskrav vid override:
- Engine-rekommendation
- Override-beslut
- Motivering
- Flaggor: `medical_context_disclosed`, `athlete_explicit_request`

Loggas i `coach_briefs` eller motsvarande. **Trixa kan inte göra manual_override** — det är specifikt en LLM-coachs prerogativ. När Trixa möter samma situation måste den följa engine eller eskalera till varning.

## Tråd-praxis

| Tråd-typ | Syfte | Frekvens |
|---|---|---|
| **Veckoplanering** | Bygg vecka N från engine + passbank + adept-status | Per vecka |
| **Uppföljning** | Mid-week check-in, justera resten av veckan | Ad hoc |
| **Passbank** | Designa/lägga till nya pass | Ad hoc |
| **Trixa-protokoll** | Designa formulär och kommunikation | När dags |
| **Arkitektur** | Refaktorera engine, adapter, datamodell | Ad hoc |

Projektet (CLAUDE.md + md-källdokument + kod) bär delad kunskap. Tråden är arbetsytan.

## Källdokument

- `1_1_Instruktioner_för_Nils_Sjöberg.md` — Nils-personlighet (basen för framtida `personas/nils.yaml`)
- `2_1_*.md` → `data/phases.yaml`
- `2_2_*.md` (sex faser) → `data/phase_details.yaml`
- `3_1_*.md` → `data/workouts.yaml`
- `3_4_*.md` → `data/overtraining.yaml`
- `3_5_*.md` → `data/strength.yaml`
- `3_6_*.md` — mental träning, ej översatt
- `3_7_*.md` — nutrition, ej översatt

## Konventioner

- **Språk:** Kodnycklar engelska/snake_case. Innehåll/labels/coachning svenska.
- **Inga LLM-anrop i Trixa-kod.** Engine, adapter, passbank, protokoll, generator — allt körs deterministiskt.
- **Engine läser, skriver inte.** Lagring sker i annat lager.
- **Beslutsdokumentation:** Engine-output har `reason`. Coach-override har `motivation`.
- **Inkrementellt bygge.** Minsta meningsfulla del först, testa, lägg till.

## Veckoplaneringsflöde (Nils)

1. Niklas öppnar ny tråd "Vecka XX — planering"
2. Claude (Nils-personlighet) läser CLAUDE.md och relevanta md-filer
3. Nils läser läget via MCP (`get_athlete`, `get_constraints`, `get_recovery`, `get_week`) — motsvarar `planner._build_athlete_state()` / `_build_ot_signals()`
4. Nils kör engine
5. **Verifierar utgångsläget** med adepten om engine flaggar något
6. Bygger veckan; om coach-beslut avviker från engine, dokumentera som manual_override
7. Om passbanken finns: välj/generera konkreta pass med .fit-export
8. Skrivs som rader i `public.planned_sessions` med `origin='nils'` (en rad per pass: date, sport på svenska, title, duration_min, intensity, details)

## Veckoplaneringsflöde (Trixa — designprincip)

1. Adept fyller i veckorapport-formulär i Trixa-appen
2. Trixa-kod läser formulär + Garmin-data, bygger engine-inputs
3. Trixa kör engine
4. Trixa följer engine strikt (ingen override-möjlighet)
5. Trixa väljer pass från passbanken via deterministiska regler
6. Trixa producerar veckoplan + .fit-filer + veckorapport-protokoll
7. Eventuella varningar (överträning, datalucke, otillräckligt utgångsläge) eskaleras som strukturerade alerts

## Öppna spår (uppdaterad 2026-05-25)

**Komplett:**
- ✓ Engine: phases, workouts, strength, overtraining
- ✓ YAML-konfig (phases, phase_details, workouts-koder, strength, overtraining, races, athlete_config — saknar bara mental + näring)
- ✓ Passbank: **124 pass + 27 drills** i `coach/data/workouts/` (swim/bike/run × 7 kategorier + brick_BW.yaml). Parametriserade mallar OCH konkreta varianter. Validerar mot SCHEMA.md.
- ✓ Renderer (markdown), validator, template-resolver, profile-loader (yaml → AthleteProfile)
- ✓ Adapter byggd och testad mot live-data
- ✓ Medicinsk kontext delad, manual_override-mönster etablerat
- ✓ Vecka 22-plan byggd av Nils (rebuild week 1, hybrid prep/build, dokumenterad override)
- ✓ Nils-persona formaliserad (`coach/personas/nils.yml`, i projektkunskap)
- ✓ Datainsamlings-pipeline dokumenterad (GitHub Actions, Knixlas/Trixa2)
- ✓ iOS Shortcut "Synka Trixa" — manuell sync från hemskärm, `sync_type=full`
- ✓ Token-health-workflow (daglig övervakning, mejlnotifiering vid >26h stale)
- ✓ Token-rotation-skript (`refresh_garmin_tokens.ps1`, ett kommando)
- ✓ Garmin sync runbook (`02_GARMIN_SYNC_RUNBOOK.md` i projektkunskap)

**TrainingPeaks-rebuild (startad 2026-06-07 — TP som enda integration):**
- ✓ Designdok + runbook (`docs/06_TP_INTEGRATION_REBUILD.md`, `docs/07_TP_SYNC_RUNBOOK.md`)
- ✓ `coach/integrations/trainingpeaks/`: `client` (auth/läs/skriv), `mapping` (passbank→TP-struktur, bike/run inkl. distansreps), `structure` (wire+IF/TSS+payload), `workout_writer` (pass→TP, AutoSync-flagga), `sync` (TP→`garmin_coach.*`-cache: HRV-baseline beräknas, PMC→load, sleep-proxy), `auth_store` (Supabase-cookie), `run_sync` (worker-CLI) — **12 tester gröna**
- ✓ Wire planner + worker: planner pushar pass till TP efter `generate_week` (gated `TRIXA_PUSH_TO_TP`); befintliga workern (`coach/trixa/cron.py`) kör daglig TP-läs-sync (gated `TRIXA_TP_SYNC`). Läs-vägen funkar via cachen. **14 tester gröna.**
- ✓ **Go-live läs-väg (2026-06-07):** TP Premium ✓, Garmin↔TP AutoSync ✓, cookie i `public.tp_auth` ✓ (RLS på). Garmin-synken var redan **död** sedan 1 juni (MFA-token, "CI utan TTY"); TP tog vid rent vid gapet (skarp `run_sync` 2–7 juni). Verifierat: TP-matad RHR/HRV/sömn + **`load_ratio` nu fylld** (0.81–1.20, var alltid NULL). Engine läser TP-datan (`tunga_lastveckor` lever). Live-fält-fixar: faktisk passtid = `totalTime` (h), sporttyp = `workoutTypeValueId`, `garmin_activity_id` = bigint (TP workoutId).
- ✓ **Garmin pensionerad (2026-06-07):** GitHub-workflowen "Garmin Sync" `disabled_manually` via `gh`, och `schedule`-triggern borttagen i `sync.yml` (workflow_dispatch kvar för rollback). Strava-resolvern lämnad som vilande fallback (läser `strava_activities`, får ingen ny data).
- ☐ Kvar (Niklas, Railway): sätt `TRIXA_TP_SYNC=1` + `TRIXA_PUSH_TO_TP=1` på workern för automatisk daglig sync + pass-push (körs manuellt tills dess); ev. Railway-garmin-worker tas bort om en sådan service finns; live-test av skriv-vägen (pass→klocka).

**AI-uppkoppling (2026-08-25):**
- ✓ `/mcp` — MCP-server (streamable HTTP, JSON-RPC 2.0) ovanpå `/agent/*`.
  Stateless, per-adept-Bearer-token, 8 verktyg. Verifierad end-to-end mot
  live-data (handskakning, tools/list, whoami, get_week, get_recovery,
  get_training_log, revoke → 401). 12 tester i `coach/tests/test_mcp_server.py`.
  Setup: `docs/11_MCP_CONNECTOR.md`.
- ✓ Claude Code / Claude Desktop / Cursor kan koppla in sig i dag
  (`claude mcp add --transport http trixa <url>/mcp --header "Authorization: Bearer …"`).
- ✓ **OAuth 2.1-auktoriseringsserver** (`trixa_api/oauth_server.py`) — claude.ai
  (webb/mobil) kan nu koppla upp sig utan token: adepten lägger till connectorn,
  loggar in, godkänner. RFC 9728-metadata + RFC 8414-metadata + DCR (RFC 7591)
  + PKCE S256 + audience-bindning (RFC 8707) + roterande refresh med
  familjeåterkallning. Migration 011. 23 tester i `test_oauth_server.py`.
  **Verifierad end-to-end mot live-data**: register → authorize → token → MCP →
  refresh-rotation → revoke dödar åtkomsten.
- Nyckeln som brukar fälla integrationer: `/mcp`:s 401 måste bära
  `WWW-Authenticate: Bearer resource_metadata="…"`, och AS-metadatan måste ha
  `code_challenge_methods_supported: ["S256"]`. Saknas något av det faller
  klienten tillbaka på att fråga om ett Client ID som ingen kan svara på.
- Personliga `trixa_`-tokens fungerar parallellt — Claude Code-vägen är orörd.
- `TRIXA_PUBLIC_URL` **måste** vara satt i prod: metadatan måste ge exakt den
  adress klienten når servern på, annars vägrar den ansluta.
- ☐ CIMD (`client_id_metadata_document_supported`) ej implementerat — DCR räcker
  för Claude, och CIMD kräver att servern hämtar ett dokument från en klientstyrd
  adress (SSRF-yta). Token i URL är INTE ett alternativ (läcker i loggar).

**Användarfynd åtgärdade (2026-09-01, rapport `trixa-elva-fynd.md`):**
Elva fynd från skarp användning via MCP + en 36-veckorsplan. TX-3 (falsk
upsert) och TX-6 (log_override 404 för självcoachade) var redan fixade i
`23edf63`. Resterande nio:
- ✓ **TX-1** `get_athlete` returnerar hela profilen (aktiva grenar, vilodagar,
  utrustning, pool, inne/ute) + nytt `get_constraints` som väger ihop de hårda
  gränserna. Var orsak till att 19 pass fick raderas manuellt.
- ✓ **TX-2** Passets innehåll renderas som markdown i en flik som heter
  "Passet" och är öppen (`trixa_api/markdown_lite.py`, escapar före format).
- ✓ **TX-4** `planned_sessions.exercises` fylls av planeraren (ur
  `strength_block`-steg) och av `plan_session`; loggformuläret förifylls.
  `coach/trixa/exercise_plan.py`. Loggraden skrivs aldrig automatiskt.
- ✓ **TX-5** `/ui/health/update` — hälsoposter redigerbara, stabila id:n.
- ✓ **TX-7** Vilodagar räknas inte som pass ("3 (+4 vila)").
- ✓ **TX-8** Aktivitetskälla-sektionen syns alltid; Anslut-knappen sätter
  `conn_*`-flaggan själv.
- ✓ **TX-9** Hinderbanelopp som distanstyp (migration 012 **applicerad 2026-09-01**).
- ✓ **TX-10** En spar-knapp; `section`-fält styr vilka delar posten bär.
- ✓ **TX-11** `get_recovery` bär `has_data` + note; serverinstruktionen täcker
  fallet utan klocka.
- ☐ Kvar: OCR-specifikt träningsinnehåll (grepp/drag/bärningar) i passbanken
  — eget passbanksarbete; radera den diagnostiska OAuth-klienten
  `trixa-client-i-_k03j9Rm3EXAUJmRgy-g`.

Lärdom: `test_settings_page` mätte först utvecklarens miljö — "Anslut
Strava"-länken kräver `STRAVA_CLIENT_ID/SECRET`, som finns i lokal `.env` men
inte i CI. Testet var grönt lokalt och rött i CI. UI-tester som beror på
miljövariabler måste sätta dem själva.

**Autoreglerad styrkeprogression (2026-09-01):**
Passbanken beskrev modellen — "progredierar nästa gång samma RIR nås vid lägre
ansträngning, detta ÄR autoreglering" (`strength_MS.yaml`) — men ingen kod bar
den. Loggen tog emot vikt och ansträngning; ingenting läste dem tillbaka.
Adepten mindes förra vikten själv och gissade nästa, som i gamla Trixa.
- `coach/trixa/strength_progression.py` — **dubbel progression**: kör inom
  protokollets repspann (`strength.yaml::protocol_parameters`), öka reps tills
  taket nås, väx då reps mot vikt och gå ned till golvet. `effort` styr takten
  (lätt +5 %, lagom +2,5 %, tungt håll, för tungt −5 %). Ökningen är aldrig
  mindre än ett faktiskt viktsteg (0,5/1/2,5 kg efter last) — annars avrundas
  progressionen bort. Tre pass på samma vikt utan att den lättat = deload −10 %.
  Loggade reps under spannets golv slår ut angiven ansträngning: kryssrutan är
  en åsikt, reps ett mätvärde. Kroppsvikt progredierar i reps. **Ingen gissad
  startvikt** — utan historik ber den om ett värde mot RIR:et.
- Repspannet följer med varje övning (`reps_min`/`reps_max`) från passets
  `parameters.reps.range` via `exercises_from_steps`. Äldre rader får ett
  smalt spann runt sitt rep-tal.
- Migration **013 applicerad 2026-09-01**: `exercise_logs.exercise_code`
  (stabil historiknyckel — namnbyte i katalogen får inte nolla progressionen)
  + två index. Namnmatchning är kvar som reserv för gamla rader.
- Samma förslag i BÅDA ytorna: adeptens loggformulär (förifyllt + en mening om
  varför) och `/agent/week` → MCP `get_week` (`exercises[].suggestion` med
  reason/trend/previous), så coachen inte föreslår vikter som motsäger appen.
  Bara loggar före passets datum räknas — ett senare pass i veckan får inte
  styra ett tidigare bakåt i tiden.
- 37 tester (`test_strength_progression.py` + `..._wiring.py`). Gränsen från
  TX-4 står kvar: formuläret förifylls, loggraden skrivs aldrig automatiskt.

**Progressionen nådde inte skarp data (2026-09-01, samma dag):**
Första skarpa kollen visade tomt. Tre orsaker, ingen i räknemodellen:
- Niklas enda styrkepass (3 sept, `origin='nils'`) hade `exercises = null` OCH
  `steps = null` — Nils skrev de sex maskinövningarna som prosa i `details`.
  Utan strukturerad lista finns ingen rad att hänga ett förslag på. **Passet
  strukturerat manuellt 2026-09-01** (koder ur `strength_exercises.yaml` där
  de fanns; `details` orörd).
- De 29 befintliga loggarna (mars–april) har `sets`/`reps`/`weight_from` =
  NULL rakt igenom, bara `effort` satt. Progressionen behöver en vikt att
  räkna från.
- Loggarna slutar 2026-04-27, sex dagar utanför 120-dagarsfönstret.

Åtgärder utöver det strukturerade passet:
- `plan_session` svarar nu med `warnings` när ett styrkepass saknar
  `exercises` (eller `reps_min`/`reps_max`). Varning, inte avslag — ett
  "Rörlighet 20 min" lagt som Styrka är ett giltigt pass utan set att bocka
  av. Verktygsbeskrivningen sade redan "skriv den ALLTID"; det räckte inte,
  så skrivningen svarar numera med vad som saknas. MCP-schemat exponerar
  också `code`, `reps_min`, `reps_max` och `load`.
- `suggestions_by_name()` + fritextformuläret: skriver adepten ett namn hen
  loggat förr fylls kod, set, reps och vikt i från historiken. Passets form
  är inte adeptens val. **Utan protokoll körs INTE dubbel progression** —
  ett spann som följer med senast loggade reps flyttar taket varje gång, så
  reps klättrar i all evighet och vikten stiger aldrig. Reps låses därför vid
  det adepten körde och progressionen sitter helt i vikten. Kroppsvikt
  (ingen loggad vikt) progredierar i reps som vanligt.

**Kodöversyn 2026-09-02 — 50 verifierade fynd, åtgärdade i sju PR:er:**
Hela listan med status per fynd: `docs/12_KODOVERSYN_2026-09-02.md`.
Det som ändrar hur man ska tänka om koden:
- **`coach/trixa/sports.py` är enda sportvokabulären.** Tretton tabeller
  ersatta av tunna vyer. Ny gren = en rad där. `walk` (Promenad) bedöms
  som vila men är en aktivitet; `brick` matchar TP:s ett-pass-brick.
- **`coach/trixa/clock.py::today()`** i stället för `date.today()` —
  Railway kör UTC; ett test vägrar nya serverlokala anrop.
- **`coach/trixa/training_log.py::dedup_cross_source`** är enda dedupen;
  bara OLIKA källor slås ihop.
- **`coach/trixa/config.py`** bär `TRIXA_DEFAULT_USER_ID` — ingen
  adept-UUID som literal någonstans; CLI:er kräver `--user` eller miljö.
- **Planeraren:** genomförda/passerade rader skrivs aldrig över vid
  regenerering; plan-skrivfel kastas (inte sväljs); Nils-grinden kastar
  vid DB-fel; fas-override appliceras FÖRE veckopositionen; perioden
  avancerar (`_current_period_estimate`); styrkan får periodposition
  (`period_position`), inte cykelposition.
- **Testfaken (`_C/_Q`) filtrerar datumintervall på riktigt** och
  utvärderar UPDATE-villkor före uppdateringen. Fixturer måste ligga
  inom sina fönster (`_RECENT` i test_agent_api).
- **Kör `pytest -q` från roten** (som CI), inte bara `coach/tests` — #43
  föll på CI efter grönt lokalt. **Och torrkör planeraren mot skarp data**
  efter ändringar i `generate_week`:
  `python -m coach.trixa.planner --athlete-user-id <id> --week-start <måndag>`
  (utan `--apply`). 337 gröna tester missade en saknad import som
  torrkörningen fann på en sekund (#50). `test_generate_week_dry_run.py`
  kör nu hela vägen mot fejken, inklusive skrivningarna.
- `/health/integrations` kräver `TRIXA_OPS_TOKEN` för detaljer.
  ACWR-gränser i `overtraining.yaml` (`acwr_high/low`).
- Alla 50 fynd åtgärdade (#39–#49). Sist in: `_prefetch_dashboard` (en
  läsning per tabell), `coach/trixa/origins.py` (origin-policy: is_human/
  reps_prescribed/athlete_deletable/swappable), `coach/engine/numbers.py`
  (to_float/to_int/positive_float), statusens utseende i `ui._STATUS`,
  `generate_week` uppdelad (_select_week_workouts, _persist_week,
  _trace_data_sources), cron läser `phase_state` i stället för RAM.

**Yoga som gren + pass som går att markera gjorda (2026-09-02):**
Skarp användning av en andra adept (Sarah, `acb82415-…`): hon lade in
"Yoga" som eget pass, valde Styrka eftersom Yoga inte fanns, och passet
stod som "Missad" morgonen efter utan väg att säga emot.
- `_PLANNED_SV_SPORT["Yoga"] = "yoga"` — egen gren, inte vila. Mappat till
  rest fick ett gjort yogapass statusen "Tränade på vilodag". Promenad/
  vandring är fortsatt vila. `_TL_SPORT`/`_RELEVANT_SPORTS` hade redan yoga.
- "Logga pass"-formuläret gäller nu även styrka och yoga ("Markera som
  gjort"); `_LOGGABLE_SPORTS` är enda listan över vad som får loggas/läggas
  in. Distans/puls visas bara för konditionsgrenar.
- Eget pass har kryssrutan "Passet är redan gjort — logga det direkt":
  gårdagens yoga inlagd i efterhand är en rapport, inte en plan. Skriver en
  training_log-rad länkad till planraden.
- `_mark_done_from_exercise_logs`: avbockade övningar (effort ≠ −1) gör ett
  "Missad" styrkepass till "Genomförd" vid läsning. Ingen rad skrivs.
  Bedömningen från en riktig training_log-aktivitet skrivs aldrig över.
- Nils kan planera yoga via MCP (`_SPORT_ENUM`, `_EN_TO_SV`).
- Sarahs rad rättad i DB (`sport` Styrka → Yoga). Hennes yoga står
  fortfarande som missad tills hon markerar den — det vet bara hon.

**Prosa-pass strukturerade + två progressionsregler (2026-09-02):**
- Sarahs tolv styrkepass (2 trixa2 från före TX-4 med bara `steps`, 10 nils
  med övningarna som prosa) har nu `exercises`. Engångsskript i
  `db/backfill/2026-09-02_structure_prose_strength.py` — parsern lever
  DÄR, inte i produkten. Tid/sträcka/"till utmattning" lämnar `reps` tomt
  och följer med i `note`. Katalogkoder satta där övningen är samma.
- **Coachens reps är en föreskrift.** `apply_suggestions(...,
  coach_prescribed=True)` för alla pass som inte är `origin='trixa2'`
  (nils/manual/NULL): reps står som skrivet, bara vikten följer
  ansträngningen. "3×10, djupet ändras först när svullnaden varit tyst två
  veckor" får inte bli 3×12 av en automat. Passbankens genererade pass får
  full dubbel progression.
- **Övningar utan rep-tal** (dödhäng, kryp, planka i sekunder) får inget
  påhittat: `reps=None`, kroppsvikt → "öka tid eller sträcka", med vikt
  (farmer's walk) → vikten progredierar som vanligt.
- Kvar: passbankens hålltider (planka `reps: 1` + tid i `load_pct`) och
  logg av sekunder (dödhäng) saknar fält — `exercise_logs` har bara reps.

**Onboarding generaliserad (2026-08-25):**
- Formuläret antog erfaren triatlet. Nu: aktiva discipliner + erfarenhetsnivå
  frågas först och styr resten. Tröskelvärden visas för advanced/elite (eller
  via kryssrutan "jag har testvärden"), nutrition bara för tri-erfarna,
  långpassdagar bara för aktiva grenar, distanslistan byggs per gren.
- Coachnamn är adeptens val (vallista + eget namn) — "Nils" är borta ur all
  adept-vänd text. Personan lever kvar i `coach/personas/nils.yml` och som
  `origin='nils'` i datamodellen.
- Besvär kan sitta på flera kroppsdelar (`locations`-lista; `location` kvar för
  äldre läsvägar) och adepten kan ange upp till 3 besvär + 2 tillstånd direkt.
- Ny kvittenssida `/ui/onboarding/klart` speglar svaren + nästa steg.
- Migration 010: `coach_name`, `onboarding_version`, vidgad `races.distance`.
  **Applicerad i Supabase 2026-08-25.**

**Pågående (Trixa-go-live-spår startat 2026-05-25):**
- ☐ Supabase: strukturerad datamodell (injuries-jsonb, health_conditions, weekly_reports, coach_overrides)
- ☐ `coach/trixa/planner.py` — knyt ihop engine + passbank + DB-skrivning
- ☐ Alert-protokoll i `data/alerts.yaml` (deterministiska eskaleringar utan LLM-tolkning)
- ☑ `.fit`-export-pipeline — **ersatt** av TP-skrivvägen (`workout_writer` → TP → Garmin AutoSync); `.fit` behålls bara som ev. nödutgång för brick/styrka
- ☐ FastAPI-skal med Nils-vänliga endpoints (`/api/week/current`, `/api/override` m.fl.)
- ☐ Trixa-formulär (HTMX/Jinja) — onboarding, hälsotillstånd, testvärden, veckorapport
- ☐ Railway-deploy (web + worker)

**Övrigt (lägre prioritet, ej blockerande):**
- ☐ Applicera patch på `setup_github_secrets.ps1` (stdin-pipe istället för `--body`) — se `01_PATCH_setup_github_secrets.md`
- ☐ Städa duplicerad `sync.yml` (en i rot, en i `.github/workflows/` — bara den senare körs)
- ☐ GitHub MCP-konnektor — för att kunna triggra Garmin-sync från Claude-tråd (alternativ till iOS Shortcut)
- ☐ Datalänkning `garmin_coach.athlete_profile.user_id` ↔ `public.profiles.id`
- ☐ Sleep-bias-hantering (kalibrering eller manuell rapportering)
- ☐ Datalucke-detektering i adapter (flagga om weekly_hours mycket lägre än deklarerat)
- ☐ Migrera från deprecated `garth`-bibliotek (ej akut)
- ☐ Översättning av md 3.6 + 3.7
- ☐ Städa gammal `coach/RENDERED_EXAMPLES.md` (ersatt av versionen i `data/workouts/`)

## Lärdomar 2026-05-25

- Passbanken är inte längre "ej byggd" — den är välspecificerad och valideringsbar. CLAUDE.md var stale; framtida trådar ska läsa `coach/data/workouts/` innan de tror på status-listan.
- Två path-buggar i `coach/engine/`: `loader.py` och `profile.py` hade `parent.parent.parent` istället för `parent.parent`. Båda fixade. `verify_and_render` + smoke-test kör grönt.
- Nils-via-Supabase-arkitekturen är fastslagen: BÅDA skriver veckoplan till MASTER `public.planned_sessions` (Trixa med `origin='trixa2'`, Nils med `origin='nils'`), Nils läser samma tabell via MCP eller Trixa-API (`/api/week/current`). Utfört läses från `public.training_log`. Override skrivs till `coach_overrides` (athlete_id = athlete_profiles.id!) med engine_recommendation + override_decision + motivation. Trixa-planner respekterar override när nästa vecka genereras och kvitterar med `honored_by_planner`.

## Lärdomar 2026-07-02 — generell filosofi + multi-user

Expertgranskning av styrdokument + passbank följdes av refaktorering
(branch `general-philosophy-multiuser`). Läget efter den:

- **Styrkeprotokollet var inverterat** (MS 10-12 reps light, SM 4-5 set heavy).
  Nu: `strength.yaml::protocol_parameters` per protokoll — MS = 3-6 reps heavy
  2-3 set 2 ggr/v, SM = 1-2 set × 6-10 1 gång/v. Planner schemalägger
  `sessions_per_week[0]` styrkepass.
- **TE var oåtkomlig** — ingen fas listade den. Nu i base_2/base_3
  (period_only + exclude_last_week) och build; `kind_of()` räknar TE som quality.
- **Vilovecko-cykeln lever**: `_resolve_period_position()` räknar veckoposition
  från `phase_state.weeks_in_phase` (skrivs tillbaka vid apply) och adeptens
  `recovery_week_ratio`. Sista cykel-veckan: hårda kategorier bort + volym × 0.6.
  CLI/API-argumenten week_in_period/weeks_in_period är numera manuell override
  (default None = auto). OBS: förut defaultade allt till "vecka 1 av 6" i drift.
- **Peak-taper**: factor 0.75/vecka (var 0.5 = detraining) + AE/SS tillbaka i peak.
- **Overtraining**: severe-flaggor väger dubbelt (`weighted_count`), severe ≥5.
- **Nutrition** flyttad från phase_details → `nutrition.yaml` (defaults) +
  athlete_profiles-överridor; planner bygger `decisions["nutrition"]` i race/
  sista peak-veckan. Niklas har Ozempic-notering i nutrition_notes.
- **Profilkedjan konsoliderad**: `coach/engine/profile.py::profile_from_athlete_row`
  är enda parsningsvägen ("2:15"-format). `planner._build_athlete_profile_for_zones`
  är ett alias (cron importerar det). lthr_bike + max_hr är inte längre hårdkodade None.
- **Buggfix som väckte sim-banken**: alla swim-YAML hade `parametrized:` (stavfel)
  → mallarna resolvades aldrig. Fixat + inline-specs (`sets: {default: N}`)
  plattas i resolve_template. Alla 116 pass renderar nu.
- **Onboarding**: signup skapar athlete_profiles-rad (`_ensure_athlete_profile`,
  idempotent), dashboard redirectar till `/ui/onboarding` tills `onboarded_at`
  är satt. Formuläret samlar trösklar (+källa), tävling → races, vecka,
  hälsa, nutrition.
- **Niklas = användare 1**: backfillad via migration 008 (max_hr 185, resting 48,
  lthr_bike 162, recovery_week_ratio **2:1** (masters), Ozempic-notering) +
  races-seed (Kalmar 2026-08-15, mål **sub-13** — races.yaml:s 11:30 var stale).
  Test-användaren fdcce15c-… verifierar väg 2 (egen plan, egna zoner, 3:1).
- **Passbank-luckan stängd senare samma dag**: brick_BW.yaml (BAE1 lång brick,
  BME1 race-pace-brick, BSS2 T2-övning; discipline=brick med `sport`-fält per
  segment, per-sport-zonrendering), AE2_bike_04 (IM-race-watt-block 68-73 %
  FTP + nutrition), AE2_run_04 (IM-pace-block) + AE2_run_05 (walk/run 9/1),
  AE2_swim_05 (OW-skills; kräver pool_type=open_water) + AE2_swim_06 (broken
  3×1000 @ CSS+4-6) + drills `sighting`/`deep_water_start`. Loader upptäcker
  brick_*.yaml; BW→brick i pass-valet; brick VINNER long_bike_day;
  session_mapping har brick-regler (resolve_session kortslöt tidigare brick).
- **Kvar från granskningen (separata trådar)**: sim-TE/ME-pacekollaps +
  zonluckor i zones.py; felräknade pass-tider i äldre pass;
  deltoid-`contraindications`-fält; test_zones-omskrivning.

## Nästa checkpoints

- **2026-05-31 (sön)**: Uppföljning vecka 22 + planering vecka 23. Adapter körs igen mot färsk data. Delta-symtom rapporteras explicit. *Lärdomar från vecka 22 dokumenteras i denna fil.*
- **Innan v 25**: Om HRV-baseline inte drivit uppåt → omkalibrera ramp-takten. Om delta-symtom kvarstår → fysio innan styrkedelen rampas vidare.
