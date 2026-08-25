# 09 — Nils dataaccess (läs- och skrivvägar)

**Syfte:** Exakta queries för Nils (Claude-projekt med Supabase MCP) sedan
datamodell-konsolideringen 2026-06-07 (docs/08). Gamla instruktioner som pekar
på `training_weeks`/`workouts`/`training_plans` ger tomma svar — **de
tabellerna är droppade**.

**Lägg in detta dokument i Nils projektkunskap** och ta bort hänvisningar till
de gamla tabellerna.

## Nycklar (Niklas)

| Vad | Värde |
|---|---|
| `user_id` (= profiles.id, nyckel i masters) | `09db449d-b8fd-409a-b475-3401b0de9858` |
| `athlete_profiles.id` (nyckel i coach_overrides) | `81b667bc-f37c-4311-a45e-1b0a28d1ada7` |
| `garmin_athlete_id` (nyckel i garmin_coach.*) | `98057fa1-4fb9-48f5-be86-b31272dcfed0` |

## Läsa veckans plan

```sql
select date, sport, title, duration_min, intensity, details, status, origin, workout_code
from public.planned_sessions
where user_id = '09db449d-b8fd-409a-b475-3401b0de9858'
  and date between '<måndag>' and '<söndag>'
order by date;
```

- `origin='nils'` = dina egna pass, `'trixa2'` = motorns, `'manual'` = adeptens egna, NULL = legacy.
- `sport` är svenska: `Cykel` / `Sim` / `Löpning` / `Styrka` / `Vila`.

## Skriva/ändra plan (Nils vinner alltid)

Skriv direkt i `planned_sessions` med `origin='nils'`:

```sql
insert into public.planned_sessions
  (user_id, date, sport, title, duration_min, intensity, details, status, origin)
values
  ('09db449d-b8fd-409a-b475-3401b0de9858', '<YYYY-MM-DD>', 'Cykel',
   '<titel>', 60, 'Z2', '<beskrivning>', 'planned', 'nils');
```

- Motorns **grind** skyddar dina dagar: den genererar aldrig pass för datum som
  redan har en rad med `origin != 'trixa2'`.
- Använd **bara** `public.planned_sessions` som skrivväg. Den äldre
  `garmin_coach.planned_workouts`-vägen är pensionerad och speglas inte längre.
- Om en `trixa2`-rad redan ligger på dagen du planerar: ändra den inte till
  `nils`. Skapa/behåll din `origin='nils'`-rad; Trixas nästa regenerering
  markerar sin egen överflödiga rad som `cancelled`, och TP-workern tar bort
  motsvarande pass från TrainingPeaks.

## Läsa utfört (genomförda pass)

Det finns TVÅ ställen — läs båda och föredra `training_log`:

**1. `public.training_log` — MASTER (primär).** Källtaggad, deduppad, rika fält.
Pass från TrainingPeaks får `source='tp'`, från Strava `source='strava'`.

```sql
select date, sport, title, duration_min, distance_km, avg_hr, max_hr,
       avg_power, normalized_power, tss, source, tp_workout_id
from public.training_log
where user_id = '09db449d-b8fd-409a-b475-3401b0de9858'
  and date >= '<datum>'
order by date desc;
```

**2. `garmin_coach.activities` — TP-RÅCACHE (fallback).** Hit landar varje
TP-synkat pass FÖRST (nyckel `athlete_id`, inte user_id). Workern propagerar
sedan genomförda pass → `training_log`. Saknas ett mycket färskt pass i
`training_log` ligger det här:

```sql
select start_time, activity_type, duration_sec/60 as min,
       round(distance_m/1000.0,1) as km, avg_hr, normalized_power, training_load
from garmin_coach.activities
where athlete_id = '98057fa1-4fb9-48f5-be86-b31272dcfed0'
  and start_time >= '<datum>'
order by start_time desc;
```

`activity_type` här är engelska (`cycling`/`running`/`swimming`/`strength_training`).

> Om ett pass syns i `garmin_coach.activities` men inte i `training_log`: synken
> har cachat det men inte hunnit propagera till mastern (sker vid nästa
> TP-sync, eller direkt när adepten trycker "Hämta från TrainingPeaks nu").

## Läsa recovery (HRV/sömn/RHR)

```sql
select metric_date, resting_hr, hrv_last_night_ms, hrv_baseline_low, hrv_baseline_high,
       sleep_score, readiness_score, stress_avg, load_ratio
from garmin_coach.daily_metrics
where athlete_id = '98057fa1-4fb9-48f5-be86-b31272dcfed0'
order by metric_date desc limit 14;
```

## Override (manual_override)

```sql
insert into public.coach_overrides
  (athlete_id, coach_user_id, scope, engine_recommendation, override_decision,
   motivation, medical_context_disclosed, athlete_explicit_request)
values
  ('81b667bc-f37c-4311-a45e-1b0a28d1ada7',          -- athlete_profiles.id, INTE user_id!
   '4e225307-ee66-4bf8-a141-69f52218e2ce',          -- Coach Svidén
   'overtraining',                                   -- week|workout|phase|volume|overtraining
   '{"level": "..."}', '{"level": "..."}',
   '<motivering, minst 10 tecken>', true, false);
```

Planeraren kvitterar respekterad override med `honored_by_planner=true` + `honored_at`.

## Via Trixa-API (alternativ till Supabase-MCP)

Tre ytor. Vill du bara koppla in en AI-klient (Claude Code, Claude Desktop,
Cursor) — hoppa direkt till `/mcp` och läs `docs/11_MCP_CONNECTOR.md`.

### `/agent/*` — per-adept-token (REKOMMENDERAS för extern AI)

Token = identitet. Adepten skapar en token i Trixa (Inställningar → "AI-åtkomst")
och lägger in den i AI-projektet. **Alla anrop låses till den adepten** — ingen
`athlete_user_id`-param, ingen risk att nå andras data. Provider-agnostiskt
(vilken AI som helst som kan HTTP + Bearer).

Auth: `Authorization: Bearer <token>`. Bara token-hashen lagras; råvärdet visas
en gång vid skapande. Återkalla när som helst i Inställningar.

| Metod | Endpoint | Gör |
|---|---|---|
| GET | `/agent/whoami` | Vilken adept är token:en scope:ad till? |
| GET | `/agent/athlete` | Mål, testvärden, hälsa |
| GET | `/agent/week/current` | Veckans plan (denna vecka) |
| GET | `/agent/week?monday=YYYY-MM-DD` | Godtycklig vecka |
| GET | `/agent/log?since=YYYY-MM-DD&limit=` | Utfört (training_log) |
| GET | `/agent/recovery?days=` | HRV/sömn/RHR/load |
| POST | `/agent/plan/session` | Skriv pass (origin='nils', upsert på dag+gren) |
| DELETE | `/agent/plan/session/{id}` | Ta bort eget pass |
| POST | `/agent/override` | Skapa manual_override |

`POST /agent/plan/session`-body: `{date, sport (bike/run/swim/strength/rest),
title, duration_min, intensity, details, workout_code}`. Läs-svar normaliserar
sport till engelska; lagring sker svenska.

### `/mcp` — MCP-server (REKOMMENDERAS för AI-klienter)

Samma per-adept-token som `/agent/*`, men talad som MCP så en AI-klient kan
koppla upp sig utan mellanled. Verktyg: `whoami`, `get_athlete`, `get_week`,
`get_training_log`, `get_recovery`, `plan_session`, `delete_planned_session`,
`log_override`. Setup och felsökning: `docs/11_MCP_CONNECTOR.md`.

```bash
claude mcp add --transport http trixa https://<trixa-url>/mcp --header "Authorization: Bearer trixa_..."
```

Fungerar inte i claude.ai (webb/mobil) än — custom connectors där kräver OAuth,
vilket är nästa etapp.

### `/api/*` — delad token (intern/admin/dev)

Äldre ytan med `athlete_user_id`-param + delad `TRIXA_API_TOKEN`. Bredare
åtkomst — använd inte för extern AI per adept; håll till `/agent/*` ovan.
- `GET /api/week/current?athlete_user_id=09db449d-...`, `GET /api/athlete/<id>`,
  `POST /api/override` m.fl.
