# Flytta Garmin-sync från GitHub Actions till Railway-worker

## Varför

Garmin invaliderar OAuth-tokens snabbare när requests kommer från GitHub
Actions IP-adresser. Single-use refresh-tokens + IP-skifte = döda tokens
efter ~1-2 dygn. Lösningen: kör sync från en stabil Railway-IP istället.

Bonus: persistent process betyder att tokens cachas mellan körningar
(inget cold-start från Secret varje gång).

## Förutsättningar

- Trixa-app ligger redan på Railway (`trixa2.up.railway.app`)
- Repo: `Knixlas/Trixa2`, branch `trixa-app-skeleton` eller `main`
- Garmin-sync är cherry-pickad till main (`sync_worker.py` + apscheduler dep)
- Supabase oauth_tokens-tabellen finns och har fresh tokens

## Skapa worker-service i Railway

1. Gå till ditt befintliga Trixa-projekt på railway.com
2. Klicka **+ New** (uppe till höger i projektet) → **GitHub Repo**
3. Välj `Knixlas/Trixa2`
4. Railway skapar en ny service. Klicka **Settings**:
   - **Service Name:** `garmin-sync-worker`
   - **Source → Branch:** `main` (eller samma som Trixa-appen)
   - **Source → Root Directory:** `garmin-mcp`
   - **Deploy → Start Command:** `python sync_worker.py`
   - **Networking:** lämna avstängt (worker behöver ingen public endpoint)

5. Klicka **Variables** och lägg in:
   ```
   GARMIN_EMAIL                = niklas@sviden.se
   GARMIN_PASSWORD             = <ditt Garmin-lösenord>
   SUPABASE_URL                = https://vtwqebihrxrufgrzmefe.supabase.co
   SUPABASE_SERVICE_ROLE_KEY   = <samma som Trixa-appen>
   SUPABASE_USER_ID            = <valfritt>
   GARMIN_TOKEN_DIR            = /app/.garminconnect
   LOG_LEVEL                   = INFO
   ```

6. Railway börjar bygga. Första körningen efter deploy:
   - Worker startar
   - Läser tokens från Supabase oauth_tokens-tabellen
   - Kör en full sync direkt (initial seed)
   - Schemalägger 06:30 UTC dagligen
   - Loggar finns under **Deployments → senaste → View logs**

## Verifiera att det funkar

```sql
-- I Supabase SQL editor:
SELECT updated_at FROM garmin_coach.oauth_tokens WHERE email = 'niklas@sviden.se';
SELECT * FROM garmin_coach.sync_log ORDER BY started_at DESC LIMIT 5;
```

Tokens.updated_at ska gå framåt vid varje körning. sync_log ska visa
`status='success'`.

## När Railway-worker är verifierad: stäng av GitHub Actions cron

Två alternativ:

### A. Behåll workflow för manuell trigger
Edit `.github/workflows/sync.yml` — ta bort `schedule`-blocket, behåll
`workflow_dispatch`. Du kan fortfarande trigga sync manuellt via iOS
Shortcut eller GitHub UI.

```yaml
on:
  workflow_dispatch:
    inputs:
      sync_type: ...
  # Borttaget:
  # schedule:
  #   - cron: '30 5 * * *'
```

### B. Disable hela workflow
Byt namn på `.github/workflows/sync.yml` → `.github/workflows/sync.yml.disabled`
så GitHub slutar köra den. Bevarad i repot ifall du vill aktivera igen.

## Felsökning

**Worker startar inte:**
- Kolla **View logs** i Railway-deployment
- Vanliga fel: saknade env-vars, fel start-command, fel Root Directory

**Sync failar med 401:**
- Tokens i `oauth_tokens` är ogiltiga
- Kör `refresh_garmin_tokens.ps1` lokalt en gång för att rotera

**MFA-prompt i loggen:**
- Tokens var helt slut (full re-login krävs)
- Workers kan inte hantera MFA — kör manuell refresh från din lokala
  PowerShell-session, sen restartar workern automatiskt och plockar upp
  färska tokens från Supabase

## Bonus: lägg till mer än daglig sync

I `sync_worker.py`, ändra `CronTrigger`-uttrycket:

```python
# Var 6:e timme istället för 1 gång per dag:
CronTrigger(hour='*/6', minute=30)

# Två gånger per dag (morgon + kväll):
scheduler.add_job(run_full_sync, CronTrigger(hour=6, minute=30))
scheduler.add_job(run_full_sync, CronTrigger(hour=18, minute=30))
```

Mer frekvent sync = mer aktuell data, men också mer token-rotation. Börja
med en daglig, eskalera bara om data-aktualiteten är ett problem.

## Migrationsväg (rekommenderad ordning)

1. Push `sync_worker.py` + uppdaterad `requirements.txt` till main
2. Skapa Railway-worker-servicen enligt instruktioner ovan
3. Verifiera att första körningen lyckas (loggar + sync_log)
4. Låt både Railway-worker och GitHub Actions cron köra parallellt 2-3 dagar
5. Verifiera att Railway-versionen är stabilare (inga 401-fel)
6. Disable GitHub Actions cron (alternativ A eller B ovan)
