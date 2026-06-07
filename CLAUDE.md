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
- **Adapter** (`coach/adapters/`): Hämtar Supabase-data, bygger engine-inputs.
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

**Tomma tabeller redo att fyllas:** `training_weeks`, `workouts`, `training_plans`, `coach_alerts`, `personal_records`, `chat_messages`.

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
│   ├── data/
│   │   ├── athlete_config.yaml
│   │   ├── races.yaml
│   │   ├── phases.yaml
│   │   ├── phase_details.yaml
│   │   ├── workouts.yaml       ← passtyp-koder (AE/ME/AC/...)
│   │   ├── strength.yaml
│   │   ├── overtraining.yaml
│   │   └── workouts/           ← passbank, EJ BYGGD ÄN
│   ├── engine/
│   │   ├── zones.py
│   │   ├── phases.py
│   │   ├── workouts.py
│   │   ├── strength.py
│   │   └── overtraining.py
│   ├── adapters/
│   │   └── garmin.py
│   ├── personas/
│   │   └── nils.yaml              ← Nils-persona (uppladdad i projektkunskap)
│   └── trixa/                  ← EJ BYGGT ÄN (formulär, protokoll, generator)
└── tests/
    └── test_smoke.py
```

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
3. Nils anropar `adapters.garmin.build_athlete_state()` och `build_overtraining_signals()`
4. Nils kör engine
5. **Verifierar utgångsläget** med adepten om engine flaggar något
6. Bygger veckan; om coach-beslut avviker från engine, dokumentera som manual_override
7. Om passbanken finns: välj/generera konkreta pass med .fit-export
8. (Senare:) skrivs som rader i `training_weeks` + `workouts`

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
- ✓ Passbank: **116 pass + 25 drills** fördelat på 21 YAML-filer i `coach/data/workouts/` (3 discipliner × 7 kategorier). Parametriserade mallar OCH konkreta varianter. Validerar mot SCHEMA.md.
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
- Nils-via-Supabase-arkitekturen är fastslagen: Trixa skriver veckoplan till `training_weeks` + `workouts`, Nils läser samma data via MCP eller Trixa-API. Override skrivs till `coach_overrides` med engine_recommendation + override_decision + motivation. Trixa-planner respekterar override när nästa vecka genereras.

## Nästa checkpoints

- **2026-05-31 (sön)**: Uppföljning vecka 22 + planering vecka 23. Adapter körs igen mot färsk data. Delta-symtom rapporteras explicit. *Lärdomar från vecka 22 dokumenteras i denna fil.*
- **Innan v 25**: Om HRV-baseline inte drivit uppåt → omkalibrera ramp-takten. Om delta-symtom kvarstår → fysio innan styrkedelen rampas vidare.
