<#
.SYNOPSIS
    Lizhaolin Math Error Notebook Web - Windows Start Script
.DESCRIPTION
    Starts the web backend, DeepSeek Harness UI, and local MySQL instance.
    Supports foreground and background daemon modes.
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Hostname = "127.0.0.1",
    [switch]$Daemon,
    [switch]$NoUI
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Math Notebook Web - Start Services (Windows)  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (Test-Path -LiteralPath $VenvPython) {
    $PythonExe = $VenvPython
    Write-Host "[OK] Using virtual environment: $PythonExe" -ForegroundColor Green
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = (Get-Command python).Source
    Write-Host "[!] Virtual environment not found, using system Python: $PythonExe" -ForegroundColor Yellow
} else {
    Write-Error "Python 3.10+ is required and must be on PATH."
    exit 1
}

$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $EnvFile) {
    Write-Host "[OK] Configuration file loaded: .env" -ForegroundColor Green
} else {
    Write-Host "[!] .env file not found (you can copy from .env.example)" -ForegroundColor Yellow
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "[!] Node.js not detected on PATH, Harness UI may not load." -ForegroundColor Yellow
}

$ArgsList = @("-X", "utf8", "-B", "scripts\local_env.py", "serve", "--host", $Hostname, "--port", $Port.ToString(), "--enable-harness-model")
if (-not $NoUI) {
    $ArgsList += "--enable-harness-ui"
}

if ($Daemon) {
    $ArgsList += "--daemon"
    Write-Host "[*] Starting services in background daemon mode..." -ForegroundColor Cyan
    & $PythonExe @ArgsList
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "[OK] Services started in background." -ForegroundColor Green
    Write-Host "     - Web URL: http://${Hostname}:${Port}" -ForegroundColor Cyan
    Write-Host "     - Log file: data\runtime\service.stdout.log" -ForegroundColor Gray
} else {
    Write-Host "[*] Starting services in foreground (Press Ctrl+C to stop)..." -ForegroundColor Cyan
    Write-Host "     - Web URL: http://${Hostname}:${Port}" -ForegroundColor Green
    & $PythonExe @ArgsList
    exit $LASTEXITCODE
}
