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
  redan har en rad med `origin != 'trixa2'`. Du behöver inte radera motorns
  rader själv — men om en `trixa2`-rad redan ligger på dagen du planerar,
  ta bort den så vyn inte visar dubbelt.
- Alternativ skrivväg: `garmin_coach.planned_workouts` (engelska discipliner
  bike/swim/run/rest) — planeraren speglar den till `planned_sessions` vid
  nästa körning. Direktskrivning i `planned_sessions` är att föredra (syns i
  appen direkt, TP-pushen plockar den).

## Läsa utfört

```sql
select date, sport, title, duration_min, distance_km, avg_hr, tss, source
from public.training_log
where user_id = '09db449d-b8fd-409a-b475-3401b0de9858'
  and date >= '<datum>'
order by date desc;
```

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

## Via Trixa-API (alternativ till MCP)

- `GET /api/week/current?athlete_user_id=09db449d-...` — veckans plan ur mastern
- `GET /api/athlete/09db449d-...` — athlete-state (hälsa, testvärden, mål)
- `POST /api/override` — skapa override
- Auth: Bearer-token (`TRIXA_API_TOKEN`)
