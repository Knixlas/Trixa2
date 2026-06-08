# 08 — Datamodell: två master-tabeller (designdok)

**Status:** design för godkännande 2026-06-07. **Ingen kod/data ändras förrän detta är godkänt.**
**Mål:** *en* källa till sanning för planerade pass och *en* för utförda — två MASTER-tabeller
som Trixa2, Nils (Claude) och mobil-Claude alla konvergerar på.

## Beslut (från Niklas)
- **Masters = befintliga legacy-tabeller:** `public.planned_sessions` (plan) + `public.training_log` (utfört).
- **Gamla GPT-Trixa-appen är AKTIV** mot dessa tabeller.

→ **Hård regel: additivt only.** Vi får lägga till *nullable* kolumner och index, men
**aldrig** döpa om, droppa eller ändra typ på befintliga kolumner, och **aldrig** skriva
över legacy-appens rader. De två apparna (gamla Trixa + Trixa2) måste samexistera.

---

## 1. Nuläge — fragmenterat (6 tabeller, 3 generationer)

| Roll | Tabeller idag | Master? |
|---|---|---|
| Planerade | `planned_sessions` (126, legacy/Nils), `workouts` (36, Trixa2), `garmin_coach.planned_workouts` (14, ?) | → **`planned_sessions`** |
| Utförda | `training_log` (2311, legacy, source-taggad), `garmin_coach.activities` (4190, TP-cache), `strava_activities` (1108, råcache) | → **`training_log`** |
| Recovery *(ej en pass-master)* | `garmin_coach.daily_metrics` | egen layer |

`training_log` är redan källtaggad: strava 2217 / chat 75 / screenshot 17 / manual 2, och
länkad till plan via `training_log.planned_session_id → planned_sessions.id`.

---

## 2. Målarkitektur

```
 Trixa2 (kod) ─ skapar pass ─┐
 Nils (Claude) ─ ändrar ─────┤→  planned_sessions  ──workout_writer──→ TP ──AutoSync──→ klocka
 mobil-Claude ─ diskuterar ──┘     (MASTER plan)            │
                                        ▲                   │ pairing
 TP / Strava / manuellt / chat ─────────┼───────────────────┘
                                        ▼
                                  training_log  (MASTER utfört)  ←─ mobil-Claude / Nils läser
```

- **Supabase = navet** (där alla tre aktörer möts). **TP = kant** (push plan → klocka; intag utfört).
- `garmin_coach.activities` / `daily_metrics` = **råcache** från TP, inte master. `activities`
  matar `training_log`; `daily_metrics` är en separat recovery-layer (HRV/sömn ≠ pass).

---

## 3. Kolumnmappning A — Trixa2-planner → `planned_sessions`

Trixa2 skriver idag `workouts` via `ScheduledWorkout.to_db_row`. Repointas till `planned_sessions`:

| `workouts` (källa) | `planned_sessions` (master) | Not |
|---|---|---|
| `athlete_id` (athlete_profiles.id) | `user_id` (profiles.id) | **mappning krävs** — planned_sessions nycklar på user_id |
| `date` | `date` | |
| `sport` | `sport` | verifiera vokabulär (swim/bike/run/strength/rest) |
| `title` | `title` | |
| `intensity` | *(→ ny kolumn `intensity` el. i `details`)* | |
| `title_simple` (passkod) | *(→ ny nullable `workout_code`)* | för spårbarhet + "byt pass" |
| `duration_minutes` | `duration_min` | |
| `steps` (main_set jsonb) | `steps` (jsonb) | ✓ samma form |
| `notes`/`details_markdown` | `details` | renderad prosa |
| `category` | `purpose` el. ny kolumn | |
| — | `status` = `'planned'` | |
| — | **ny nullable `origin`** = `'trixa2'` | skilj Trixa2-genererat från Nils/legacy-skapat |

**Additiva kolumner på `planned_sessions`:** `workout_code text`, `intensity text`, `origin text`
(alla nullable). Legacy-appen ignorerar dem.

`training_weeks`/`training_plans` (Trixa2-scaffolding): behövs inte av planned_sessions-modellen
→ pensioneras (se §7). Fas/period kan bäras i en nullable `phase`-kolumn på planned_sessions om
vi vill ha spårbarhet, annars i `coach_briefs`.

---

## 4. Kolumnmappning B — TP utfört → `training_log`

TP:s v6-passobjekt (verifierat live, se docs/06 §2) → `training_log`:

| TP-fält | `training_log` | Not |
|---|---|---|
| `startTime` / `workoutDay` | `date` | |
| `workoutTypeValueId` | `sport` | id→namn (1=swim,2=bike,3=run,…) |
| `title` | `title` | |
| `totalTime` (h) ×60 | `duration_min` | faktisk tid |
| `distance` /1000 | `distance_km` | |
| `heartRateAverage` / `Maximum` | `avg_hr` / `max_hr` | |
| `powerAverage` / `normalizedPowerActual` | `avg_power` / `normalized_power` | |
| `tssActual` | `tss` | |
| — | `source` = `'tp'` | |
| `workoutId` | **ny nullable `tp_workout_id bigint`** | idempotens + dedup |
| (paired plan) | `planned_session_id` | länk om TP/match hittar plan |
| råobjekt | `extra_data` (jsonb) | |

**Additiv kolumn på `training_log`:** `tp_workout_id bigint` (nullable) + **unikt partiellt index**
`(user_id, tp_workout_id) where tp_workout_id is not null` → idempotent upsert av TP-rader.

---

## 5. Dedup & skrivar-koordination (den kritiska biten)

**Problemet:** `training_log` skrivs redan av legacy-appen (strava, 2217 rader, uppdaterad 2026-06-06).
Om Trixa2:s TP-sync *också* skriver samma fysiska pass → dubbletter och dubbelräknad volym.

**Regler:**
1. **Idempotens:** TP-rader upsertas på `tp_workout_id` → samma pass skrivs aldrig två gånger av TP.
2. **Korskälls-dedup:** innan en TP-rad skapas, kolla om rad redan finns för
   `(user_id, date, sport)` med varaktighet inom ±10 % (= strava-raden för samma pass).
   - Finns en → **skriv inte ny TP-rad.** Valfritt: *berika* befintlig rad med fält TP har men
     strava saknar (power, NP, tss) utan att röra source. (Berikning = senare iteration; v1 = skippa.)
   - Finns ingen → skapa TP-rad (source='tp').
3. **Rollfördelning v1:** legacy-strava förblir primär utförd-källa (appen lever); **TP fyller
   luckor** strava missar + framtida pass. När legacy-appen ev. pensioneras kan TP bli primär.

> Öppen fråga: vill du på sikt göra **TP** till primär utförd-källa (rikare: watt/struktur) och
> fasa ut strava→training_log? Det kräver att legacy-appens strava-sync stängs av — inte nu.

---

## 6. Repoint: engine + läsare

- **Engine-adapter** (`coach/engine/garmin.py` / planner-fetcharna): veckovolym läses idag från
  `garmin_coach.activities`. Repointas till **`training_log`** (master) — `duration_min` summerad
  per 4-veckorsfönster. Recovery-signaler (HRV/sömn/RHR/load) **stannar** i `daily_metrics`
  (separat layer, ej pass-master). Engine-kontrakten (`AthleteState`/`OvertrainingSignals`) oförändrade.
- **Trixa2 dashboard/API:** läs plan från `planned_sessions`, utfört från `training_log`.
  (Dashboarden föredrar redan `planned_sessions` enligt tidigare lärdom — delvis på plats.)
- **workout_writer:** push:ar `planned_sessions` (origin='trixa2', status='planned') → TP → klocka.

---

## 7. Pensioneras (efter repoint + verifiering)

| Tabell | Åtgärd |
|---|---|
| `public.workouts` | retire — Trixa2 skriver planned_sessions istället |
| `public.training_weeks` / `training_plans` | retire — scaffolding behövs ej (ev. behåll `phase` som kolumn) |
| `garmin_coach.planned_workouts` | retire — **men identifiera först vem som skriver den** (växte 7→14 under sessionen) |
| `garmin_coach.activities` | behåll som TP-råcache som matar training_log (eller retire när training_log matas direkt) |
| `strava_activities` | behåll som legacy-råcache (matar training_log via legacy-appen) |

Inget droppas förrän vi verifierat att varken Trixa2 eller legacy-appen läser det.

---

## 8. Säker migrationsordning (legacy live hela tiden)

1. **Additiva kolumner/index** (icke-brytande): `planned_sessions += workout_code, intensity, origin`;
   `training_log += tp_workout_id` + unikt partiellt index.
2. **Repoint TP-sync** → skriv `training_log` (source='tp', dedup §5, idempotent). Sluta behandla
   `garmin_coach.activities` som master (kvar som cache).
3. **Repoint engine-adapter** → veckovolym från `training_log`; recovery oförändrat.
4. **Repoint Trixa2-planner** → skriv `planned_sessions` (origin='trixa2'). Kör parallellt med
   `workouts` en kort period för jämförelse.
5. **Repoint dashboard/API-läsningar** → planned_sessions + training_log.
6. **workout_writer** → push planned_sessions → TP.
7. **Verifiera** mot livedata + att legacy-appen är opåverkad → **retire** dubbletterna (§7).

Varje steg är reversibelt (config/branch). Legacy-appens tabeller rörs aldrig destruktivt.

---

## 9. Risker / öppna frågor

- **Två skrivare på `training_log`** (legacy-strava + Trixa2-TP) → dedup (§5) är obligatoriskt.
- **Två skrivare på `planned_sessions`** (legacy/Nils + Trixa2) → `origin`-tagg + Trixa2 rör bara
  egna rader (clobbra aldrig en Nils/legacy-rad).
- **`user_id` vs `athlete_id`:** planned_sessions/training_log nycklar på `user_id` (profiles.id);
  Trixa2 internt på `athlete_id` (athlete_profiles.id). Mappning krävs i alla repoints.
- **Sport-vokabulär** kan skilja mellan systemen → normaliseringstabell.
- **Vem skriver `garmin_coach.planned_workouts`?** (14 rader, framtida datum) — identifiera innan retire.
- **TP→plan-pairing:** hur `training_log.planned_session_id` sätts (TP:s native pairing vs match
  på date+sport) — design i bygg-fasen.

---

## 10. Vad som INTE ingår
- Recovery/wellness (`daily_metrics`) — orthogonalt, ingen pass-master.
- Att pensionera legacy-appen — den är aktiv; vi samexisterar.

---

## 11. Legacy-fix (Knixlas/Trixa) — stoppa strava-dubbletter ⚠️

**Bakgrund:** `training_log` hade **1109 dubblettrader** (samma strava-aktivitet
inlagd 10–37 ggr → veckovolym 10–30× för hög). Rotorsak: gamla appens
strava→`training_log`-sync gör **plain INSERT utan idempotens**. Städat 2026-06-07
(behöll en rad per `(user_id, strava_id)`, 2311 → 1202 rader, migration
`dedup_training_log_strava_duplicates`).

**Måste fixas i gamla appen (annars återkommer dubbletterna):**
1. Gör strava-inserten **idempotent**: `upsert` på `(user_id, strava_id)` i stället
   för plain INSERT (eller "check exists before insert").
2. När (1) är på plats: lägg ett unikt index för att garantera det permanent:
   ```sql
   create unique index concurrently if not exists training_log_user_strava_uniq
     on public.training_log (user_id, strava_id) where strava_id is not null;
   ```

> ⚠️ Lägg **inte** det unika indexet före steg (1) — om gamla appen gör plain
> INSERT börjar de inserten ERRORa vid krock, vilket kan störa legacy-appen.

Tills detta är gjort: Trixa2-läsare dedupar defensivt (engine klar, se
`_fetch_actual_weekly_hours`), och dedup-migrationen kan köras om vid behov.
