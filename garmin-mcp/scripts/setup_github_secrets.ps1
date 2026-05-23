<#
.SYNOPSIS
Saetter alla GitHub Secrets som workflowen behoever.

.PREREQS
- gh CLI inloggad
- test_connection.py maaste ha koerts framgaangsrikt en gaang
  (skapar ~/.garminconnect/garmin_tokens.json)
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
Write-Host ""

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "[FEL] gh CLI saknas." -ForegroundColor Red
    exit 1
}

$null = & gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FEL] gh ej inloggat. Koer: gh auth login" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $envPath)) {
    Write-Host "[FEL] Hittar inte $envPath" -ForegroundColor Red
    exit 1
}

$tokenFile = Join-Path $TokenDir "garmin_tokens.json"
if (-not (Test-Path $tokenFile)) {
    Write-Host "[FEL] Token-fil saknas: $tokenFile" -ForegroundColor Red
    Write-Host "Koer 'python test_connection.py' foerst."
    exit 1
}

$tokenSize = (Get-Item $tokenFile).Length
if ($tokenSize -eq 0) {
    Write-Host "[FEL] Token-fil ar tom: $tokenFile" -ForegroundColor Red
    exit 1
}
Write-Host "Token-fil OK: $tokenSize bytes" -ForegroundColor Green

function Get-EnvValue([string]$key) {
    $line = Get-Content $envPath | Where-Object { $_ -match "^\s*$key\s*=" }
    if (-not $line) { throw "Hittade inte $key i .env" }
    return ($line -replace "^\s*$key\s*=\s*", "").Trim('"').Trim("'").Trim()
}

Write-Host "-> Laeser .env"
$garminEmail    = Get-EnvValue "GARMIN_EMAIL"
$garminPassword = Get-EnvValue "GARMIN_PASSWORD"

Write-Host "-> Laeser tokens"
$tokensJson = [IO.File]::ReadAllText($tokenFile)

Write-Host ""
Write-Host "Oeppna i webblaesaren:" -ForegroundColor Yellow
Write-Host "  https://supabase.com/dashboard/project/vtwqebihrxrufgrzmefe/settings/api-keys"
Write-Host "Kopiera 'service_role' (INTE 'anon')."
Write-Host ""
$secure = Read-Host "Klistra in service role key" -AsSecureString
$supabaseKey = [System.Net.NetworkCredential]::new("", $secure).Password

if ([string]::IsNullOrWhiteSpace($supabaseKey)) {
    Write-Host "[FEL] Tom key" -ForegroundColor Red
    exit 1
}

function Set-GhSecret([string]$name, [string]$value) {
    Write-Host ("  {0,-32} " -f $name) -NoNewline
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "gh"
    $psi.Arguments = "secret set $name --repo $Repo --body -"
    $psi.RedirectStandardInput  = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $bytes = [Text.Encoding]::UTF8.GetBytes($value)
    $proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $proc.StandardInput.Close()
    $proc.WaitForExit()

    if ($proc.ExitCode -eq 0) {
        Write-Host "OK" -ForegroundColor Green
    } else {
        Write-Host "FEL" -ForegroundColor Red
        $errOut = $proc.StandardError.ReadToEnd().Trim()
        if ($errOut) { Write-Host "    $errOut" -ForegroundColor Red }
    }
}

Write-Host ""
Write-Host "-> Saetter secrets paa $Repo"
Set-GhSecret "GARMIN_EMAIL"               $garminEmail
Set-GhSecret "GARMIN_PASSWORD"            $garminPassword
Set-GhSecret "GARMIN_TOKENS_JSON"         $tokensJson
Set-GhSecret "SUPABASE_URL"               $SupabaseUrl
Set-GhSecret "SUPABASE_SERVICE_ROLE_KEY"  $supabaseKey

Write-Host ""
Write-Host "[KLART]" -ForegroundColor Green
Write-Host ""
Write-Host "Trigga workflowen:"
Write-Host "  gh workflow run sync.yml --repo $Repo -f sync_type=profile" -ForegroundColor Cyan
