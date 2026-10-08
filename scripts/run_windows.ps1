# Run the bot + native MT5 engines on Windows. Reads secrets from .env in the repo root.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\run_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .venv\Scripts\python.exe)) { Write-Host "Avval scripts\setup_windows.ps1 ni ishga tushiring." -ForegroundColor Red; exit 1 }
if (-not (Test-Path .env)) { Write-Host ".env topilmadi." -ForegroundColor Red; exit 1 }

foreach ($line in Get-Content .env -Encoding UTF8) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $k, $v = $t.Split("=", 2)
    [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe -m tradegenius.main
