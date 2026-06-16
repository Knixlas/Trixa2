# 07 — TrainingPeaks-sync: runbook & go-live

Driftguide för TP-som-enda-integration. Arkitektur: `docs/06_TP_INTEGRATION_REBUILD.md`.

## Pipeline

```
Garmin-klocka → Garmin Connect → (AutoSync) → TrainingPeaks → run_sync.py → Supabase
                                                     ↑                         (garmin_coach.*)
                                  workout_writer ────┘ (planerade pass → klockan via AutoSync)
```

Trixa rör bara TP. `garmin_coach.activities` + `daily_metrics` är nu en intern
cache som **fylls från TP**, inte från Garmin. Engine/adapter läser dem oförändrat.

## Engångs-steg vid go-live (Niklas — kan inte automatiseras)

1. **TP Premium** aktivt (krävs för att planera pass på framtida datum).
2. **Koppla Garmin Connect ↔ TP** i TP:s inställningar (AutoSync). Slå på både
   - aktiviteter + dagliga hälsometrik **Garmin → TP**, och
   - strukturerade pass **TP → Garmin** (nästa 15 dagar).
3. **Cookie-tabellen `public.tp_auth` är redan skapad** (RLS på, bara service-role
   kommer åt — verifierat mot livedatabasen 2026-06-07). Du behöver bara lägga in
   cookien i steg 4.
4. **Capture cookien** och lagra den:
   - Logga in på app.trainingpeaks.com → DevTools (F12) → Application → Cookies
     → kopiera värdet på `Production_tpAuth`.
   - Lagra cookien per användare via CLI (stdin gör att den inte hamnar i
     shell-historiken):
     ```bash
     python -m coach.integrations.trainingpeaks.auth_store --user <profiles.id>
     ```
     Klistra därefter in cookie-värdet och avsluta stdin.
5. **Verifiera**:
   ```bash
   python -m coach.integrations.trainingpeaks.run_sync --days 3 --dry-run
   ```
   ska visa `[daily] success`, `[activities] success` och
   `[training_log] success` utan auth-fel.

## Schemalagd sync — befintlig worker, två env-flaggor

Hela TP-integrationen körs av den **redan deployade** Railway-workern
(`worker: python -m coach.trixa.cron`) — ingen ny service behövs. Två flaggor,
default **av** tills go-live:

| Env-flagga | Effekt |
|---|---|
| `TRIXA_TP_SYNC=1` | daglig läs-sync TP→Supabase (recovery + aktiviteter) vid `TRIXA_TP_SYNC_HOUR_UTC` (default 05) |
| `TRIXA_PUSH_TO_TP=1` | planner pushar veckans pass till TP efter `generate_week(apply=True)` |

Manuell körning (verifiering / engångs):
```bash
python -m coach.integrations.trainingpeaks.run_sync --days 2           # läs-sync
python -m coach.integrations.trainingpeaks.run_sync --days 3 --dry-run # utan DB-skrivning
```
Läs-synken skriver `daily_metrics` (HRV/RHR/sömn-proxy + PMC→load),
råcachen `garmin_coach.activities` och utfört-mastern `public.training_log`.
Skriv-vägen (pass→klocka) går via `workout_writer` → TP → Garmin AutoSync.

**Go-live-flippen:** när cookien + AutoSync är på plats — sätt båda flaggorna i
Railway-workerns Variables, starta om servicen, verifiera mot livedata, kör
sedan parallellt med Garmin-workern 2–3 dagar innan du stänger av den.

## Token-rotation (cookien dör efter ~veckor)

Symptom: `run_sync` rapporterar `error=... Cookie utgången/ogiltig` eller
`TPAuthError`. Åtgärd (~1 min):
1. Capture ny `Production_tpAuth` (steg 4 ovan).
2. Kör auth_store-kommandot ovan igen för rätt `profiles.id`.

Detta är den lätta efterföljaren till Garmins MFA-token-dans: veckor i stället
för dagar, ingen MFA, ingen TTY-begränsning.

## Hälsoövervakning

`GET /health/integrations` visar senaste beständiga TP-synk per användare och
svarar 503 om någon senaste körning misslyckats eller är äldre än 30 timmar.
Körningen loggas i `public.integration_runs`.

## Kända begränsningar (verifiera mot livedata)

- `sleep_score` är en **proxy** ur sömntimmar (TP får inte Garmins 0-100-score).
- `readiness_score` lämnas tom (Garmin-proprietärt) → engine degraderar
  konservativt; kan fyllas av athletens veckorapport.
- **Brick & Strength** når inte klockan via AutoSync — skapas i TP, flaggas.
- TP:s exakta GET-fältnamn för metrics/aktivitetstid är defensivt hanterade i
  `sync.py`; finjustera när första livedatat kommit in.

## Rollback

Återstarta `garmin-mcp`-cronen och peka planner mot Garmin-direktsynk. Tabell-
schemat är gemensamt → återgång är en konfigändring, ingen datamigrering.
