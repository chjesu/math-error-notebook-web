[CmdletBinding()]
param(
    [switch]$EnableCodexModel,
    [switch]$NoServe
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is required and must be available on PATH."
}

$mysqlHome = if ($env:LZLM_LOCAL_MYSQL_HOME) {
    $env:LZLM_LOCAL_MYSQL_HOME
} else {
    "C:\Program Files\MySQL\MySQL Server 8.4"
}
if (-not (Test-Path -LiteralPath (Join-Path $mysqlHome "bin\mysqld.exe"))) {
    throw "MySQL 8.4 was not found. Install it or set LZLM_LOCAL_MYSQL_HOME."
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & python -m venv (Join-Path $root ".venv")
}

& $venvPython -X utf8 -B -m pip install -r (Join-Path $root "requirements.txt")
if ($LASTEXITCODE) { throw "Python dependency installation failed." }

& $venvPython -X utf8 -B scripts\local_env.py init
if ($LASTEXITCODE) { throw "Local MySQL initialization failed." }

& $venvPython -X utf8 -B scripts\local_env.py smoke
if ($LASTEXITCODE) { throw "Local smoke test failed." }

if ($NoServe) {
    Write-Host "Initialization complete. Run scripts\bootstrap_local.ps1 to start the Web service."
    exit 0
}

$serveArgs = @("-X", "utf8", "-B", "scripts\local_env.py", "serve")
if ($EnableCodexModel) {
    if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
        throw "Codex CLI is required when -EnableCodexModel is used."
    }
    $serveArgs += "--enable-codex-model"
}
Write-Host "Open http://127.0.0.1:8000 after the server starts."
& $venvPython @serveArgs
