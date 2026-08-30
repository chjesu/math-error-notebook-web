<#
.SYNOPSIS
    Lizhaolin Math Error Notebook Web - Windows Stop Script
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Math Notebook Web - Stop Services (Windows)  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$PythonExe = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

Write-Host "[*] Stopping local database and sessions..." -ForegroundColor Yellow
$StopOutput = & $PythonExe -X utf8 -B (Join-Path $ProjectRoot "scripts\local_env.py") stop 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Stop failed; recovery state was preserved." -ForegroundColor Red
    $StopOutput | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    exit 1
}
Write-Host "[OK] All services have been stopped successfully." -ForegroundColor Green
