# One-time setup on Windows (also Windows on ARM VMs): x64 Python venv + MetaTrader 5.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

# MetaTrader5 ships only win_amd64 wheels, so Python must be x64 (emulated fine on ARM).
$candidates = @(@("py", "-3.12"), @("py", "-3.11"), @("python"))
$py = $null
foreach ($cand in $candidates) {
    $exe = $cand[0]; $pre = @($cand | Select-Object -Skip 1)
    try {
        $machine = & $exe @pre -c "import platform; print(platform.machine())" 2>$null
        if ($LASTEXITCODE -eq 0 -and $machine -eq "AMD64") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host "x64 Python 3.11/3.12 topilmadi. O'rnating va skriptni qayta ishga tushiring:" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12 --architecture x64"
    exit 1
}
Write-Host "[setup] Python: $($py -join ' ') (AMD64)"

if (-not (Test-Path .venv\Scripts\python.exe)) {
    $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
    & $exe @pre -m venv .venv
}
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt -r requirements-wine.txt
.venv\Scripts\python.exe -c "import MetaTrader5, pandas, cryptography; print('[setup] MetaTrader5', MetaTrader5.__version__, '| pandas', pandas.__version__)"

$mt5 = "C:\Program Files\MetaTrader 5\terminal64.exe"
if (-not (Test-Path $mt5)) {
    Write-Host "[setup] MetaTrader 5 o'rnatilmoqda..."
    $installer = Join-Path $env:TEMP "mt5setup.exe"
    Invoke-WebRequest "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $installer
    Start-Process $installer -ArgumentList "/auto" -Wait
    if (-not (Test-Path $mt5)) { Write-Host "MT5 o'rnatilmadi - mt5setup.exe ni qo'lda ishga tushiring." -ForegroundColor Red; exit 1 }
}
Write-Host "[setup] MT5: $mt5"

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "[setup] .env yaratildi - TELEGRAM_BOT_TOKEN, ADMIN_IDS, ENCRYPTION_KEY, GEMINI_API_KEY ni to'ldiring." -ForegroundColor Yellow
}
Write-Host "[setup] tayyor. Ishga tushirish:  powershell -ExecutionPolicy Bypass -File scripts\run_windows.ps1" -ForegroundColor Green
