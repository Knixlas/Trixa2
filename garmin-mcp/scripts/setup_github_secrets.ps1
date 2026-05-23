<#
.SYNOPSIS
Saetter alla GitHub Secrets som workflowen .github/workflows/sync.yml behoever.

.DESCRIPTION
Laeser GARMIN_EMAIL och GARMIN_PASSWORD fraan garmin-mcp/.env.
Base64-encodar oauth1_token.json + oauth2_token.json fraan ~/.garminconnect/.
Fraagar dig en gaang om Supabase service role key.
Pushar allt till GitHub Secrets via gh CLI.

.PREREQS
- gh CLI:        winget install GitHub.cli
- gh inloggad:   gh auth login
- test_connection.py maaste ha koerts framgaangsrikt en gaang lokalt

.EXAMPLE
PS> cd C:\...\Trixa2\garmin-mcp
PS> .\scripts\setup_github_secrets.ps1
#>
[CmdletBinding()]
param(
    [string]$Repo = "Knixlas/Trixa2",
    [string]$SupabaseUrl = "https://vtwqebihrxrufgrzmefe.supabase.co",
    [string]$TokenDir = (Join-Path $HOME ".garminconnect")
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$envPath    = Join-Path $projectDir ".env"

Write-Host "=== GitHub Secrets setup for $Repo ===" -ForegroundColor Cyan
Write-Host "Projekt:  $projectDir"
Write-Host ""

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "[FEL] gh CLI saknas." -ForegroundColor Red
    Write-Host "Installera foerst:  winget install GitHub.cli"
    Write-Host "Logga sedan in:     gh auth login"
    exit 1
}

$null = & gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FEL] gh aer inte inloggat. Koer:  gh auth login" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $envPath)) {
    Write-Host "[FEL] Hittar inte $envPath" -ForegroundColor Red
    Write-Host "Koer scriptet fraan garmin-mcp-mappen."
    exit 1
}

foreach ($file in @("oauth1_token.json", "oauth2_token.json")) {
    $path = Join-Path $TokenDir $file
    if (-not (Test-Path $path)) {
        Write-Host "[FEL] Token saknas: $path" -ForegroundColor Red
        Write-Host "Koer 'python test_connection.py' foerst."
        exit 1
    }
}

function Get-EnvValue([string]$key) {
    $line = Get-Content $envPath | Where-Object { $_ -match "^\s*$key\s*=" }
    if (-not $line) { throw "Hittade inte $key i .env" }
    return ($line -replace "^\s*$key\s*=\s*", "").Trim('"').Trim("'").Trim()
}

Write-Host "-> Laeser .env"
$garminEmail    = Get-EnvValue "GARMIN_EMAIL"
$garminPassword = Get-EnvValue "GARMIN_PASSWORD"

Write-Host "-> Encodar tokens"
function Get-Base64File([string]$path) {
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes($path))
}
$oauth1 = Get-Base64File (Join-Path $TokenDir "oauth1_token.json")
$oauth2 = Get-Base64File (Join-Path $TokenDir "oauth2_token.json")

Write-Host ""
Write-Host "Oeppna detta i webblaesaren:" -ForegroundColor Yellow
Write-Host "  https://supabase.com/dashboard/project/vtwqebihrxrufgrzmefe/settings/api-keys"
Write-Host "Kopiera vaerdet under 'service_role' (INTE 'anon')."
Write-Host ""
$secure = Read-Host "Klistra in service role key" -AsSecureString
$supabaseKey = [System.Net.NetworkCredential]::new("", $secure).Password

if ([string]::IsNullOrWhiteSpace($supabaseKey)) {
    Write-Host "[FEL] Tom service role key - avbryter." -ForegroundColor Red
    exit 1
}

function Set-GhSecret([string]$name, [string]$value) {
    Write-Host ("  {0,-32} " -f $name) -NoNewline
    $value | & gh secret set $name --repo $Repo --body - 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "OK" -ForegroundColor Green
    } else {
        Write-Host "FEL" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "-> Saetter secrets paa $Repo"
Set-GhSecret "GARMIN_EMAIL"               $garminEmail
Set-GhSecret "GARMIN_PASSWORD"            $garminPassword
Set-GhSecret "GARMIN_OAUTH1_TOKEN"        $oauth1
Set-GhSecret "GARMIN_OAUTH2_TOKEN"        $oauth2
Set-GhSecret "SUPABASE_URL"               $SupabaseUrl
Set-GhSecret "SUPABASE_SERVICE_ROLE_KEY"  $supabaseKey

Write-Host ""
Write-Host "[KLART]" -ForegroundColor Green
Write-Host "Verifiera paa: https://github.com/$Repo/settings/secrets/actions"
Write-Host ""
Write-Host "Naesta: trigga workflowen fraan GitHub-appen, eller direkt haer:"
Write-Host "  gh workflow run sync.yml --repo $Repo -f sync_type=profile" -ForegroundColor Cyan
