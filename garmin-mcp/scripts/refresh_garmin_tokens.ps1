<#
.SYNOPSIS
    Förnya Garmin-tokens när de gått ut. Ett kommando, hela kedjan.

.DESCRIPTION
    När GitHub Actions börjar fala med "Cachade tokens funkar inte" är det dags
    att rotera. Det här skriptet gör hela ceremonin:

    1. Aktiverar venv i garmin-mcp/
    2. Kör test_connection.py (kräver MFA-kod från Garmin)
    3. Verifierar att token-filen är giltig JSON
    4. Pushar tokens till GitHub Secrets via stdin (inte --body, viktigt!)
    5. Kör en test-workflow och rapporterar resultat

    Bör köras från Trixa2-rotmappen eller från garmin-mcp/scripts/.

.NOTES
    Token-rotation behövs typiskt var X:e vecka när Garmin invaliderar
    refresh-token. Då måste du svara på en MFA-prompt manuellt — den
    delen kan inte automatiseras eftersom Garmin kräver kod från
    mobil/SMS.

.EXAMPLE
    .\refresh_garmin_tokens.ps1
#>

[CmdletBinding()]
param(
    [string]$Repo = "Knixlas/Trixa2",
    [string]$GarminMcpDir = $null
)

$ErrorActionPreference = "Stop"

# Hitta garmin-mcp-mappen
if (-not $GarminMcpDir) {
    $candidates = @(
        ".\garmin-mcp",
        "..\garmin-mcp",
        $PSScriptRoot,
        (Split-Path $PSScriptRoot -Parent)
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "test_connection.py")) {
            $GarminMcpDir = (Resolve-Path $c).Path
            break
        }
    }
}

if (-not $GarminMcpDir -or -not (Test-Path (Join-Path $GarminMcpDir "test_connection.py"))) {
    Write-Host "[FEL] Kan inte hitta garmin-mcp-mappen." -ForegroundColor Red
    Write-Host "      Kör skriptet från Trixa2-roten eller ange -GarminMcpDir."
    exit 1
}

Write-Host "=== Garmin Token Refresh ===" -ForegroundColor Cyan
Write-Host "Repo:         $Repo"
Write-Host "garmin-mcp:   $GarminMcpDir"
Write-Host ""

# Steg 1: aktivera venv
$venvActivate = Join-Path $GarminMcpDir ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvActivate)) {
    Write-Host "[FEL] Hittar inte venv på $venvActivate" -ForegroundColor Red
    Write-Host "      Skapa den först: python -m venv .venv; pip install -r requirements.txt"
    exit 1
}

Push-Location $GarminMcpDir
try {
    Write-Host "-> Aktiverar venv..." -ForegroundColor Yellow
    & $venvActivate

    # Steg 2: kör test_connection.py (interaktiv MFA)
    Write-Host "-> Kör test_connection.py (MFA-prompt kommer)" -ForegroundColor Yellow
    Write-Host "   Ha Garmin-appen redo för MFA-kod." -ForegroundColor Gray
    Write-Host ""
    python test_connection.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FEL] test_connection.py failade. Avbryter." -ForegroundColor Red
        exit 1
    }
}
finally {
    Pop-Location
}

# Steg 3: verifiera token-filen
$tokenFile = Join-Path $HOME ".garminconnect\garmin_tokens.json"
if (-not (Test-Path $tokenFile)) {
    Write-Host "[FEL] Token-filen saknas: $tokenFile" -ForegroundColor Red
    exit 1
}

$tokenSize = (Get-Item $tokenFile).Length
if ($tokenSize -lt 100) {
    Write-Host "[FEL] Token-filen är misstänkt liten ($tokenSize bytes)" -ForegroundColor Red
    exit 1
}

$tokenContent = Get-Content $tokenFile -Raw
try {
    $parsed = $tokenContent | ConvertFrom-Json -ErrorAction Stop
    if (-not $parsed.di_token -or -not $parsed.di_refresh_token) {
        Write-Host "[FEL] di_token eller di_refresh_token saknas i token-filen" -ForegroundColor Red
        exit 1
    }
    Write-Host "-> Token-fil OK: $tokenSize bytes, di_token + di_refresh_token närvarande" -ForegroundColor Green
}
catch {
    Write-Host "[FEL] Token-filen är inte giltig JSON: $_" -ForegroundColor Red
    exit 1
}

# Steg 4: pusha till GitHub via stdin (KRITISKT: inte --body)
Write-Host "-> Pushar GARMIN_TOKENS_JSON till $Repo..." -ForegroundColor Yellow
$tokenContent | gh secret set GARMIN_TOKENS_JSON --repo $Repo
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FEL] gh secret set failade" -ForegroundColor Red
    exit 1
}
Write-Host "   GARMIN_TOKENS_JSON pushad OK" -ForegroundColor Green

# Steg 5: trigga test-workflow och rapportera
Write-Host "-> Triggar test-workflow (sync_type=profile)..." -ForegroundColor Yellow
$runOutput = gh workflow run sync.yml --repo $Repo -f sync_type=profile 2>&1
Write-Host $runOutput

Write-Host ""
Write-Host "Väntar 30 sek på att workflow ska slutföras..." -ForegroundColor Gray
Start-Sleep -Seconds 30

# PowerShell 5.1 (Windows default) kan inte parsa --key=value mot externa
# kommandon — använd whitespace-separerade args istället.
$latestRun = gh run list --workflow sync.yml --repo $Repo --limit 1 --json databaseId,status,conclusion | ConvertFrom-Json
$run = $latestRun[0]

# Extrahera värden till lokala variabler — $()-subexpression i strängar
# fungerar inte alltid stabilt i PS 5.1.
$runId = $run.databaseId
$runStatus = $run.status
$runConclusion = $run.conclusion

Write-Host ""
Write-Host "Senaste korning: ID $runId" -ForegroundColor Cyan
Write-Host "Status:          $runStatus"
Write-Host "Resultat:        $runConclusion"

if ($runConclusion -eq "success") {
    Write-Host ""
    Write-Host "[KLART] Tokens fungerar. Synken ar operativ igen." -ForegroundColor Green
    exit 0
}
elseif ($runStatus -eq "in_progress") {
    Write-Host ""
    Write-Host "[VANTA] Korningen pagar fortfarande. Kolla manuellt om en stund:" -ForegroundColor Yellow
    Write-Host "        gh run view $runId"
    exit 0
}
else {
    Write-Host ""
    Write-Host "[FEL] Workflow failade trots ny token-push. Kolla loggar:" -ForegroundColor Red
    Write-Host "      gh run view $runId --log-failed"
    exit 1
}
