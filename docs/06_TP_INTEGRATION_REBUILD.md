# 06 — TrainingPeaks som enda integration (rebuild)

**Status:** design fastslagen 2026-06-07, bygge pågår
**Mål:** Trixa2 ska bara prata med **ett** externt system — TrainingPeaks (TP).
Garmin kvarstår *som klocka*, men Trixa rör aldrig Garmins API. Garmins egen
TP-koppling (AutoSync) blir bryggan åt båda håll.

Bakgrund: Garmins inofficiella API kräver kortlivade MFA-tokens som dör med
några veckors mellanrum och inte kan förnyas headless (ingen TTY i CI). TP:s
auth är en cookie som lever i *veckor* och kan läggas i en env-variabel — en
betydligt billigare driftbörda. Se [[trixa-datasource-landscape]] för
landskapsanalysen som ledde hit.

---

## 1. Topologi

```
                 Garmin Connect ──AutoSync──▶  TrainingPeaks  ◀──── Trixa (läser)
   Garmin-klocka ─┘  (aktiviteter,            (enda nav)      ────▶ Trixa (skriver pass)
        ▲             hälsodata: HRV,                │
        │             sömn, RHR, BB)                 │
        └──────────── AutoSync (strukturerade pass, nästa 15 dagar) ◀┘
```

- **Inåt (läsa):** Garmin-klockan synkar som vanligt till Garmin Connect.
  Garmin Connect AutoSync skickar aktiviteter **och dagliga hälsometrik** vidare
  till TP. Trixa läser TP.
- **Utåt (skriva):** Trixa skapar strukturerade pass i TP-kalendern. TP→Garmin
  AutoSync levererar nästa 15 dagars pass till klockan. Ändringar propagerar.
- **Trixa↔TP är hela integrationsytan.** Inget Garmin-API, ingen Strava-OAuth i
  den löpande driften.

Förutsättning som athleten gör **en gång**: koppla Garmin Connect ↔ TP i TP:s
inställningar (OAuth, engångs). Därefter är bryggan server-till-server och rör
inte Trixa.

---

## 2. Datatäckning — vad TP faktiskt ger engine

Engine läser via en `query(sql, params)`-callable mot Supabase-tabeller. Vi byter
**producenten** av de tabellerna från Garmin-sync till TP-sync; engine-koden rörs
inte. Tabellen nedan är den bärande verkligheten — vad TP levererar per fält som
`coach/engine/garmin.py` läser.

### `daily_metrics` (overtraining-signaler)

| Engine-fält | TP-källa | Status | Anmärkning |
|---|---|---|---|
| `resting_hr` | metric `pulse` (type 5) | ✅ | RHR via Garmin→TP wellness |
| `hrv_last_night_ms` | metric `hrv` (type 60) | ✅ | nattlig HRV (rMSSD-ms) |
| `hrv_baseline_low` / `_high` | — | ⚠️ **beräknas i sync** | TP saknar baseline. Vi räknar rullande 60-dagars medel ± SD och skriver det. |
| `hrv_weekly_avg_ms` | — | ✅ härleds | 7-dagars medel av hrv |
| `sleep_score` (0–100) | metric `sleep` = **timmar** (type 6) | ⚠️ **proxy** | TP får sömntimmar, inte Garmins 0–100-score. Sync deriverar en proxy-score från timmar tills annat bekräftas. *Öppet: verifiera om Garmins sleep score korsar AutoSync under annan type-id.* |
| `readiness_score` | — | ⚠️ degraderar | Garmins Training Readiness är proprietär och korsar troligen inte. → NULL ⇒ `_feels_rested()` blir False (konservativt). Kan fyllas av athletens veckorapport. |
| `stress_avg` | (Body Battery/stress crossar enligt TP) | ▫️ valfri | Adaptern läser men *använder inte* i signalerna. Ofarlig om NULL. |
| `acute_load` | PMC **ATL** | ✅ **NY** | Var NULL under Garmin-sync. |
| `chronic_load` | PMC **CTL** | ✅ **NY** | |
| `load_ratio` | **ATL/CTL** (≈ACWR) | ✅ **NY** | Driver `_consecutive_high_load_weeks`. Tidigare alltid None ⇒ den signalen var död. TP *väcker* den. |

### `activities` (veckovolym)

| Engine-fält | TP-källa | Status |
|---|---|---|
| `duration_sec` | workout `totalTimeActual` → sek | ✅ |
| `start_time` | workout `workoutDay` / `startTime` | ✅ |
| `athlete_id` | TP `athleteId` → mappas till befintlig garmin_coach-uuid | ✅ |

**Nettoeffekt:** Engines *kärnbeslut* (fas från veckotimmar + tävlingsnedräkning +
skada + OT; överträning från RHR-delta, HRV-%, sömnstreak, load-veckor) täcks.
Vi **vinner** load-signalen (ATL/CTL/ACWR). Vi **tappar fidelity** på Garmins
proprietära scores (readiness, sleep score, HRV-baseline) — dessa beräknas om,
proxas, eller degraderar konservativt. Det är priset för ett enda nav, och det är
medvetet och dokumenterat, inte tyst.

> **Beslutspunkt för Niklas:** om readiness/sleep-score visar sig väga för tungt
> i praktiken är alternativet att athleten själv rapporterar mående i
> veckorapporten (redan en planner-input) snarare än att återinföra ett
> Garmin-beroende. Vi börjar med graceful degradation + beräknade substitut.

---

## 3. Auth & token (headless)

Cookie-baserat, inget lösenord. Flödet (portat från MCP:n):

1. `Production_tpAuth`-cookie hämtas en gång från webbläsaren (DevTools →
   Application → Cookies på app.trainingpeaks.com).
2. Cookien växlas mot en kortlivad OAuth-token: `GET /users/v3/token` →
   `{token:{access_token, expires_in:3600}}`. Bearer används sedan på alla anrop.
3. Token cachas i minnet och förnyas automatiskt 60 s före utgång.

**Lagring (prioordning i klienten):**
1. env `TP_AUTH_COOKIE`  ← **detta är headless-vägen (Railway-worker)**
2. OS-keyring (lokal dev)
3. krypterad fil (AES-256-GCM)

**Drift:** cookien lever typiskt *veckor*. När den dör returnerar klienten
`AUTH_EXPIRED` → token-health varnar → athleten kör om engångs-capturen (~1 min)
och uppdaterar Railway-secreten. Vi speglar Garmin-token-mönstret men med veckors
intervall i stället för dagars. Lagras i Supabase-tabell `tp_auth` (en rad) så att
både worker och web läser samma cookie. Se task 8 / runbook.

---

## 4. Endpoint-referens (bekräftad mot källkod)

Bas: `https://tpapi.trainingpeaks.com`

| Endpoint | Metod | Syfte |
|---|---|---|
| `/users/v3/token` | GET | cookie → access_token |
| `/users/v3/user` | GET | `personId`, `athletes[]` → athlete-id |
| `/fitness/v6/athletes/{id}/workouts/{start}/{end}` | GET | pass i datumintervall (≤90 d) |
| `/fitness/v6/athletes/{id}/workouts/{wid}` | GET/PUT/DELETE | enskilt pass |
| `/fitness/v6/athletes/{id}/workouts` | POST | **skapa pass** |
| `/fitness/v1/athletes/{id}/reporting/performancedata/{start}/{end}` | POST | **CTL/ATL/TSB** (body: `{atlConstant:7, ctlConstant:42, ...}`) |
| `/metrics/v3/athletes/{id}/consolidatedtimedmetrics/{start}/{end}` | GET | **hälsometrik** (hrv/pulse/sleep/…) |
| `/metrics/v3/athletes/{id}/consolidatedtimedmetric` | POST | logga metrik |

Throttle: ≥150 ms mellan anrop. 401 → rensa token, försök igen en gång.

Metric type-ids: pulse=5, sleep(h)=6, weight=9, hrv=60, spo2=53, steps=58.
Sport-ids (family,value): Swim(1), Bike(2), Run(3), Brick(4), Crosstrain(5),
Race(6), DayOff(7), MtnBike(8), Strength(9), Custom(10), XCSki(11), Rowing(12),
Walk(13), Other(100).

---

## 5. Moduler som byggs

Allt under `coach/integrations/trainingpeaks/` — deterministiskt, **ingen LLM**.

| Modul | Roll | Plugg |
|---|---|---|
| `client.py` | Sync HTTP-klient: auth, läs (workouts/metrics/PMC), skriv (create/update/delete workout). Port av MCP-klienten, utan async/MCP-beroenden. | fristående |
| `mapping.py` | passbank `main_set` → TP `SimpleWorkoutStructure` (steps/repetition, intensityClass, zon→%-intervall per disciplin). Återanvänder IF/TSS- + wire-logik. | renderaren får en 3:e output |
| `sync.py` | TP → Supabase. Skriver `garmin_coach.activities` + `daily_metrics` (samma schema engine läser) + `sync_log`. Beräknar HRV-baseline, mappar PMC→load, deriverar sleep-proxy. Idempotent upsert. | ersätter `garmin-mcp/sync_engine.py` som producent |
| `workout_writer.py` | WeekPlan/workouts → TP planerade pass via `client`. Brick/Strength skrivs men flaggas (når ej klockan via AutoSync). Idempotent. | ersätter den aldrig-byggda `.fit`-exporten |

Inkopplingspunkter i befintlig kod (task 7):
- `coach/trixa/planner.py`: `_resolve_activity_sources`, `_fetch_actual_weekly_hours`,
  `_fetch_garmin_metrics`, `_build_athlete_state`, `_build_ot_signals` → läser
  TP-matade tabeller. Efter `generate_week(apply=True)` → anropa `workout_writer`.
- `coach/engine/garmin.py`: oförändrad (läser samma tabeller). Bevaras som
  kontrakt; ev. byt namn på modulen senare.

---

## 6. Tabellstrategi

**Fas 1 (nu):** TP-sync skriver in i de *befintliga* tabellerna engine redan
läser — `garmin_coach.activities` och `garmin_coach.daily_metrics` — med samma
`athlete_id`. Noll ändringar i engine/adapter/planner-läsningar. Snabbast och
mest reversibelt. Schemanamnet `garmin_coach` är då bara ett legacy-namn på en
intern cache; *integrationen* är 100% TP.

**Fas 2 (senare, valfri städning):** källneutralt schema (`metrics.activities`,
`metrics.daily`) med `source`-kolumn, och repointa planner-frågorna. Inte
blockerande för go-live.

---

## 7. Skriv-väg: pass → klocka

1. Planner genererar `WeekPlan` (befintligt).
2. `mapping.py` översätter varje pass `main_set` → TP-struktur. Zon→intensitet
   per disciplin: bike `percentOfFtp` (watt primärt), run `percentOfThresholdPace`
   eller `percentOfThresholdHr`, swim `percentOfThresholdPace` (CSS). En
   `sets`-rad med `rest_sec` expanderas till ett `repetition`-block med
   arbets-steg + vilo-steg.
3. `workout_writer.py` POST:ar till TP med `workoutDay`, sport-ids,
   `totalTimePlanned` (**timmar**, decimal), `structure` (JSON). IF/TSS
   auto-beräknas.
4. TP→Garmin AutoSync levererar.

**AutoSync-behörighet:** Run/Bike/Swim/Crosstrain/MtnBike/Rowing/Walk/Custom/Other
når klockan. **Brick & Strength gör det inte** — brick modelleras som bike+run
back-to-back för leverans; styrka skrivs i TP (synligt i appen) men flaggas som
"når ej klockan". **TP Premium** krävs för att planera pass på framtida datum
(gratis: bara idag/imorgon) — go-live förutsätter Premium-konto.

---

## 8. Vad som pensioneras

- `garmin-mcp/` schemalagd sync (cron i `.github/workflows/sync.yml`) → **av**.
  Koden behålls (historik/backfill) men kör inte i drift.
- Strava-resolver i `planner.py` → demoteras till nödläge/av.
- `refresh_garmin_tokens.ps1`, token-health för Garmin → ersätts av TP-motsvarighet.

Inget raderas i detta skede — allt är reversibelt via git tills TP-vägen är
verifierad mot livedata.

---

## 9. Migrationssekvens

1. ✅ Studera MCP-klient + verifiera TP-täckning *(klart)*
2. ⏳ Design (detta dokument)
3. `client.py` + enhetstester (mockad HTTP)
4. `mapping.py` (passbank → wire) + test
5. `sync.py` → skriv tabeller, kör mot livedata i dry-run
6. `workout_writer.py` → skriv ett testpass till TP, verifiera att det når klockan
7. Wire planner/adapter → kör full `generate_week` mot TP-matad data
8. Token-storage + runbook
9. Tester gröna
10. Pensionera Garmin/Strava-cron, uppdatera CLAUDE.md

**Manuellt engångssteg (Niklas):** (a) koppla Garmin↔TP AutoSync i TP, (b) capture
`Production_tpAuth` → Railway-secret, (c) bekräfta TP Premium. Detta är den enda
delen koden inte kan göra själv.

**Rollback:** slå på Garmin-cron igen + repointa planner till `garmin_coach.*`
direkt-sync. Tabellschemat är gemensamt, så återgång är en konfigändring.

---

## 10. Öppna punkter / risker

- **Sleep score & readiness:** verifiera mot livedata vad som faktiskt korsar
  Garmin→TP. Om Garmins sleep score/readiness inte finns: proxy + veckorapport.
- **Reverse-engineered API:** TP kan ändra interna endpoints. Mildras av att
  `.fit`/manuell väg behålls som nödutgång och att klienten är liten och
  lättpatchad.
- **TP Premium-gräns** för framtida pass — bekräfta abonnemang.
- **Cookie-livslängd** i praktiken — mät; sätt token-health-tröskel därefter.
- **Brick/Strength** når ej klockan via AutoSync — hanteras explicit i writern.
