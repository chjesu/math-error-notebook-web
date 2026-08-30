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
& $PythonExe -X utf8 -B (Join-Path $ProjectRoot "scripts\local_env.py") stop | Out-Null

$PidFile = Join-Path $ProjectRoot "data\runtime\service.pid"
if (Test-Path -LiteralPath $PidFile) {
    try {
        $SavedPid = Get-Content -Path $PidFile -Raw
        if ($SavedPid -match '^\d+$') {
            $TargetProcess = Get-Process -Id ([int]$SavedPid) -ErrorAction SilentlyContinue
            if ($TargetProcess) {
                Write-Host "[*] Stopping background service process (PID: $SavedPid)..." -ForegroundColor Yellow
                Stop-Process -Id ([int]$SavedPid) -Force -ErrorAction SilentlyContinue
            }
        }
        Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
    } catch {}
}

$Ports = @(8000, 3080, 3307)
foreach ($Port in $Ports) {
    $Connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($Conn in $Connections) {
        if ($Conn.OwningProcess -gt 0) {
            Write-Host "[*] Releasing port $Port (PID: $($Conn.OwningProcess))..." -ForegroundColor Yellow
            Stop-Process -Id $Conn.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

if ($Force) {
    Stop-Process -Name uvicorn, node, mysqld -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
Write-Host "[OK] All services have been stopped successfully." -ForegroundColor Green
