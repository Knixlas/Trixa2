# Garmin Sync — Runbook & Troubleshooting

Tillägg till `CLAUDE.md`. När Niklas eller en framtida instans av Nils stöter på
sync-problem: läs detta först.

---

## Arkitektur (kort)

```
Garmin Connect (cloud)
        │
        │  (refresh-token i GitHub Secrets)
        ▼
GitHub Actions — sync.yml (schemalagd + manuell)
        │
        │  (Supabase service role key)
        ▼
Supabase: garmin_coach.{activities, daily_metrics, sync_log}
        │
        ▼
Nils (coach-skiktet) — läser via Supabase MCP
```

Tre saker måste fungera samtidigt:
1. **Garmin-tokens** (giltiga, i `GARMIN_TOKENS_JSON`-secret)
2. **Supabase-secrets** (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`)
3. **GitHub Actions workflow** (`sync.yml` triggas via cron eller manuellt)

---

## Diagnostik (steg 1: alltid kör detta först)

```sql
SELECT started_at, sync_type, status, records_synced, error_message
FROM garmin_coach.sync_log
WHERE athlete_id = '98057fa1-4fb9-48f5-be86-b31272dcfed0'
ORDER BY started_at DESC LIMIT 10;
```

**Tolkning:**

- Senaste rad har `status='success'` och är <24h gammal → allt rullar. Om förväntad
  data saknas, problemet ligger i Garmin Connect (klockan ej synkad till mobilen?)
- Senaste rad är `failed` med "Cachade tokens funkar inte" → kör
  `refresh_garmin_tokens.ps1` (se nedan).
- Inga rader på flera dagar → workflow har inte triggats. Kolla GitHub Actions.
- Senaste rad är `success` men inget nytt → klockan har inte synkat till Garmin
  Connect på mobilen. Inte ditt fel.

**Anti-pattern:** kolla aldrig `MAX(created_at)` på `activities` eller
`daily_metrics`-tabellerna för att avgöra om synken funkar. De tabellerna
uppdateras bara när det finns *ny* data — frånvaro av nya rader betyder inte
att synken är trasig. Kolla alltid `sync_log` istället.

---

## Token rotation: när Garmin invaliderar refresh-token

### Symptom

`sync_log.error_message` säger:
> Cachade tokens funkar inte och vi ar i CI utan TTY. Kor 'python test_connection.py' lokalt...

### Åtgärd

Ett kommando från Trixa2-roten:

```powershell
.\garmin-mcp\scripts\refresh_garmin_tokens.ps1
```

Skriptet:
1. Aktiverar venv i `garmin-mcp/`
2. Kör `test_connection.py` (du svarar på MFA-prompten)
3. Verifierar att token-filen är giltig JSON
4. Pushar `GARMIN_TOKENS_JSON` till GitHub via stdin (inte `--body`)
5. Triggar test-workflow och rapporterar resultat

Förväntad tid: ~2 min, varav ~30 sek är MFA-väntan.

### Varför kan vi inte automatisera bort MFA?

Garmins login kräver MFA-kod från mobil/SMS vid token-rotation. GitHub Actions
har ingen TTY, ingen webbläsare, ingen möjlighet att läsa SMS. TOTP-secret stöds
inte av Garmins normala API-flöde.

**Det här är en kostnad vi får leva med.** Lösningar med headless browser eller
sparade MFA-secrets är fragila och bryter Garmins ToS.

---

## Den klassiska bug:en: `gh secret set --body $value` strippar citationstecken

PowerShell tar bort `"` från strängar som passeras som argument till externa
program. Vid push av JSON-secrets med `gh secret set NAME --body $value` hamnar
trasig data på GitHub:

```
Filen på disk:  {"di_token": "...", "di_refresh_token": "..."}  (2024 bytes)
Pushat secret:  {di_token: ..., di_refresh_token: ...}          (2016 bytes, -8 bytes = 8 stripped quotes)
```

**Lösning: använd alltid stdin-pipe.**

```powershell
# RÄTT:
$value | gh secret set NAME --repo $Repo
# eller
Get-Content $path -Raw | gh secret set NAME --repo $Repo

# FEL:
gh secret set NAME --body $value --repo $Repo
gh secret set NAME --body-file $path --repo $Repo   # även detta strippar!
```

`refresh_garmin_tokens.ps1` använder rätt mönster. `setup_github_secrets.ps1`
behöver patchas — se `docs/01_PATCH_setup_github_secrets.md`.

---

## Proaktiv hälsoövervakning

`.github/workflows/token-health.yml` körs varje morgon kl 06:30 UTC. Den
verifierar att senaste lyckade sync är <26 h gammal. Om inte → workflow failar,
GitHub mejlar repo-ägaren.

Detta är den första larmraden. Den ersätter "vi märker först när Nils råkar
fråga".

---

## Manuell trigger-cheatsheet

```powershell
# Synka aktiviteter (det vanligaste behovet)
gh workflow run sync.yml --repo Knixlas/Trixa2 -f sync_type=activities

# Synka allt
gh workflow run sync.yml --repo Knixlas/Trixa2 -f sync_type=full

# Synka bara dagens metrics (HRV, sömn, readiness)
gh workflow run sync.yml --repo Knixlas/Trixa2 -f sync_type=daily

# Bara profil (test att auth funkar — lättast om man bara vill veta)
gh workflow run sync.yml --repo Knixlas/Trixa2 -f sync_type=profile
```

`sync_type`-alternativ enligt `.github/workflows/sync.yml`:
`daily`, `activities`, `profile`, `full`.

`days`-parametern (default 1) styr hur många dagar bakåt `daily` hämtar.
`activities_limit` (default 20) styr hur många aktiviteter `activities` hämtar.

---

## Två kopior av sync.yml

Det finns för närvarande två filer:
- `Trixa2\sync.yml` (rotmapp)
- `Trixa2\.github\workflows\sync.yml` (den som faktiskt körs)

Identisk storlek 25/5 (4654 bytes). Bara den i `.github/workflows/` körs av
GitHub Actions. Den i roten är troligen historisk/duplikat. Värt att städa.

---

## Kända småbuggar (ej akuta)

- **`diagnose_tokens.py`** kraschar på `client.sess`-attribut som inte finns i
  installerad `garth`-version. Skriptet visar ändå tokens innan kraschen, så
  diagnostiskt värde är intakt.
- **`garth`** är deprecated enligt biblioteket självt (se
  https://github.com/matin/garth/discussions/222). Inte akut, men på sikt
  behöver vi migrera till `garminconnect`-modulen direkt eller annan klient.
- **`test_connection.py`** visar `Namn: ?` för user profile — fältet
  `displayName`/`fullName` returneras inte längre korrekt. Auth funkar ändå.

---

## Sammanfattning för framtida Nils

Om sync är trasig:
1. Kör SQL mot `garmin_coach.sync_log` (steg 1 ovan)
2. Om token-fel → instruera Niklas att köra `refresh_garmin_tokens.ps1`
3. Om annat fel → läs `error_message`-fältet och `gh run view <id> --log-failed`
4. Påverka aldrig veckoplaneringen pga sync-strul — det är infrastruktur, inte
   träning. Checkpoint-data kan rapporteras manuellt om det måste.
