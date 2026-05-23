# Garmin MCP – grund för en kodbaserad triathloncoach

En lokal **Model Context Protocol-server** som ger Claude (eller annan MCP-klient) läsåtkomst till dina Garmin Connect-data. Den fungerar som datalagret för en kodbaserad triathloncoach och kompletteras senare av regler/heuristik, träningsplaner och analysverktyg.

## Vad servern kan idag

| Verktyg | Beskrivning |
|---|---|
| `list_activities` | Senaste passen (filtrera på sport) |
| `get_activity_details` | Detaljer, splits och HR-zoner för ett pass |
| `get_weekly_summary` | Veckosammanställning per sport + total träningsbelastning |
| `get_training_status` | Productive / Maintaining / Peaking / Overreaching … |
| `get_training_readiness` | Dagens beredskap (0–100) med delfaktorer |
| `get_hrv_status` | HRV-status och baseline |
| `get_sleep` | Sömnfaser, score, andning |
| `get_vo2max` | VO2max löpning + cykling |
| `get_user_profile` | Profil, vilo-HR, max-HR |
| `get_heart_rate_zones` | HR-zoner per sport |

## Installation

Kräver Python 3.10+.

```bash
git clone <repo> garmin-mcp
cd garmin-mcp
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Fyll i GARMIN_EMAIL och GARMIN_PASSWORD i .env
```

### Första inloggningen (MFA)

Om du har tvåfaktor påslaget på Garmin behöver du köra en första interaktiv inloggning så att OAuth-tokens cachas:

```bash
python server.py
# Du blir promptad efter MFA-kod – klistra in den från mail/SMS
# Tokens sparas i ~/.garminconnect och återanvänds tills de går ut (~1 år)
```

Stoppa servern (Ctrl+C) efter att inloggningen lyckats – framtida starter kör utan prompt.

## Koppla in i Claude Desktop

Lägg till i `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/absolut/sökväg/garmin-mcp/.venv/bin/python",
      "args": ["/absolut/sökväg/garmin-mcp/server.py"]
    }
  }
}
```

Starta om Claude Desktop. Verktygen syns under verktygsmenyn och du kan börja ställa frågor som:

- *"Sammanfatta min träningsvecka och kommentera fördelningen mellan sporterna."*
- *"Hur ser min HRV och readiness ut idag? Bör jag göra ett tröskelpass?"*
- *"Visa mina tre senaste löppass med splits."*

## Säkerhet

- Lösenordet ligger i `.env` lokalt och skickas bara till Garmin.
- Tokens sparas i `~/.garminconnect` med användarens behörigheter.
- Lägg `.env` och `~/.garminconnect` i `.gitignore` om du versionshanterar.
- Detta är ett **inofficiellt** API – Garmin kan bryta det när som helst. Använd med eftertanke och förvänta dig periodiskt underhåll.

## Roadmap

Nästa steg när den här grunden funkar:

1. **Fler verktyg**: `get_body_battery`, `get_stress`, `get_personal_records`, `get_race_predictions`, `get_lactate_threshold`.
2. **Skrivåtkomst**: skapa workouts och pusha dem till klockan via `upload_workout`.
3. **Coach-lager**: separat MCP eller modul som givet data från Garmin-MCP:n returnerar passförslag (regelbaserat först, ML senare).
4. **Strukturerad träningsplan**: lagring i SQLite/Supabase, periodisering, races som ankarpunkter.
5. **Wahoo/Zwift/Strava-integration** om relevanta data saknas i Garmin.

## Sync till Supabase

Förutom MCP-servern finns `sync.py` som drar data från Garmin in i Supabase-schemat `garmin_coach`. Schemat består av fyra tabeller: `athlete_profile`, `activities`, `daily_metrics` och `sync_log`. Det är helt isolerat från andra scheman och avsett som datalager för den logikstyrda coachen.

### Förutsättningar

1. Lägg till i `.env`:
   ```
   SUPABASE_URL=https://<project-ref>.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=<service-role-nyckel>
   SUPABASE_USER_ID=<valfritt: auth.users-id>
   ```
   Service role-nyckeln hittar du i Supabase-dashboarden → Project Settings → API. **Den ger full åtkomst – håll den hemlig.**

2. `pip install -r requirements.txt` (drar in `supabase>=2.8.0`).

### Kommandon

```bash
python sync.py profile                                    # Uppdatera atletprofil
python sync.py activities --limit 50                      # Senaste 50 passen
python sync.py daily                                      # Dagens metrics
python sync.py daily --date 2025-11-15                    # Specifik dag
python sync.py daily --from 2025-11-01 --to 2025-11-15    # Datumintervall (backfill)
python sync.py full --activities 100 --days 30            # Allt: profil + 100 pass + 30 dagar
```

Sync är **idempotent**: upserts sker på `garmin_activity_id` resp. `(athlete_id, metric_date)`, så att upprepade körningar inte duplicerar. Varje körning loggas i `garmin_coach.sync_log` med start/slut, antal rader och eventuellt fel.

### Vanliga körscenarier

- **Första gången**: `python sync.py full --activities 200 --days 90` för att backfilla ~3 mån
- **Daglig körning (cron)**: `python sync.py daily && python sync.py activities --limit 10`
- **Felsökning**: kolla `select * from garmin_coach.sync_log order by started_at desc limit 20`

## Körning via GitHub Actions (från mobilen)

Sync kan triggas direkt från GitHub-appen på mobilen, eller schemaläggas att köra varje morgon. Workflowen ligger i `.github/workflows/sync.yml` (i repo-roten, inte i `garmin-mcp/`).

### Engångs-setup

1. **Lokal inloggning först.** Kör `python test_connection.py` på din egen maskin. Det skapar `~/.garminconnect/oauth1_token.json` och `oauth2_token.json` som behövs i molnet (MFA kan inte göras från GitHub-runners).

2. **Encoda tokens.**
   ```bash
   ./scripts/encode_tokens.sh
   ```
   Scriptet skriver ut två base64-strängar.

3. **Lägg in secrets i GitHub.** Repo → Settings → Secrets and variables → Actions → New repository secret. Lägg in följande:

   | Namn | Värde |
   |---|---|
   | `GARMIN_EMAIL` | Din Garmin-epost |
   | `GARMIN_PASSWORD` | Ditt Garmin-lösenord |
   | `GARMIN_OAUTH1_TOKEN` | Base64-strängen från encode-scriptet |
   | `GARMIN_OAUTH2_TOKEN` | Base64-strängen från encode-scriptet |
   | `SUPABASE_URL` | `https://vtwqebihrxrufgrzmefe.supabase.co` |
   | `SUPABASE_SERVICE_ROLE_KEY` | Från Supabase dashboard → API |
   | `SUPABASE_USER_ID` | Valfritt – auth.users-id, eller lämna tomt |

4. **Klart.** Workflowen kan nu triggas.

### Manuell körning från mobilen

I GitHub-appen: repo → Actions → "Garmin Sync" → Run workflow. Du får tre val:

- **sync_type**: `daily` (default), `activities`, `profile`, eller `full`
- **days**: antal dagar bakåt (för daily/full)
- **activities_limit**: antal aktiviteter att hämta

### Schemalagd körning

Cron körs automatiskt **05:30 UTC varje dag** (06:30 svensk sommartid, 07:30 vintertid). Den hämtar de senaste 10 passen och daily metrics för igår + idag.

Vill du ändra tid: redigera cron-uttrycket i `sync.yml` (`'30 5 * * *'`).

### Token-förnyelse

Garmins OAuth-tokens håller ungefär ett år. När syncen börjar fallera med autentiseringsfel:

1. Kör `python test_connection.py` lokalt igen (gör eventuell MFA-prompt om det krävs)
2. Kör `./scripts/encode_tokens.sh`
3. Uppdatera `GARMIN_OAUTH1_TOKEN` och `GARMIN_OAUTH2_TOKEN` i GitHub Secrets
