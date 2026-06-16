# Patch: setup_github_secrets.ps1

## Vad är problemet?

Skriptet pushar GitHub secrets via `gh secret set NAME --body $value` (förmodligen
inuti en `Set-GhSecret`-helper-funktion). Det är PowerShells string-mangling som
strippar citationstecken (`"`) ur värdet innan `gh` får det. Resultat: secret
hamnar på GitHub men är trasig JSON.

Detta är **grundbug:en** som kostade en timmes felsökning 25/5 — `GARMIN_TOKENS_JSON`,
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` blev alla trasiga vid varje setup-körning.

## Diagnos: så här upptäcker du om buggen finns kvar

```powershell
# Pusha en tokens-fil
gh secret set TEST_SECRET --repo Knixlas/Trixa2 --body (Get-Content $HOME\.garminconnect\garmin_tokens.json -Raw)

# Workflow som läser TEST_SECRET kommer se trasig JSON: {di_token: ...} istället för {"di_token": ...}
```

`gh secret set --body` med en sträng-argument är **alltid** osäkert i PowerShell.

## Fix

Ersätt **alla** anrop av formen:

```powershell
gh secret set NAME --body $value --repo $repo
# ELLER
Set-GhSecret "NAME" $value
# ELLER
gh secret set NAME --body-file $path --repo $repo
```

Med stdin-pipe:

```powershell
$value | gh secret set NAME --repo $repo
# ELLER för fil:
Get-Content $path -Raw | gh secret set NAME --repo $repo
```

### Hela `Set-GhSecret`-funktionen, korrekt version:

```powershell
function Set-GhSecret {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value,
        [string]$Repo = "Knixlas/Trixa2"
    )

    # KRITISKT: använd stdin-pipe, inte --body.
    # PowerShell strippar citationstecken från string-argument när de skickas
    # till externa program — vilket bryter JSON-secrets.
    $Value | gh secret set $Name --repo $Repo
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $Name".PadRight(35) -NoNewline
        Write-Host "OK" -ForegroundColor Green
    } else {
        Write-Host "  $Name".PadRight(35) -NoNewline
        Write-Host "FEL" -ForegroundColor Red
        throw "gh secret set $Name failade"
    }
}
```

### Verifiering efter patch

Efter att patchen applicerats, kör:

```powershell
.\scripts\setup_github_secrets.ps1
gh workflow run sync.yml --repo Knixlas/Trixa2 -f sync_type=profile
# Vänta 30 sek
gh run list --workflow="sync.yml" --limit 1 --json conclusion
# Förväntat: "conclusion": "success"
```

Om workflowet är grön — buggen är fixad. Om felmeddelandet säger
"Secret är inte giltig JSON" är patchen inte applicerad korrekt.

## Bonus: validera tokens innan push

Lägg också in en sanity-check som **läser** secret tillbaka och verifierar att
JSON är intakt direkt efter push. Annars upptäcker man bug:en först när workflow
failar långt senare. Förslag:

```powershell
# Efter Set-GhSecret för GARMIN_TOKENS_JSON:
Write-Host "  -> Verifierar att JSON pushats korrekt..." -NoNewline
# Vi kan inte läsa tillbaka secret (det är ju en hemlighet) men vi kan verifiera
# att filen på disk vi precis pushade fortfarande är giltig JSON:
try {
    $parsed = $tokensJson | ConvertFrom-Json -ErrorAction Stop
    if ($parsed.di_token -and $parsed.di_refresh_token) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " FEL: di_token/di_refresh_token saknas" -ForegroundColor Red
    }
} catch {
    Write-Host " FEL: ogiltig JSON i token-fil" -ForegroundColor Red
}
```
