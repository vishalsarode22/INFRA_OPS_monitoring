<#
    Start InfraBeatOps the way it should run in normal operation:
    - no --reload (that is what was making the UI sluggish; see RUN.md)
    - bound to localhost:8000
    - refuses to double-start if something already holds the port

    Use this instead of a hand-typed `uvicorn ... --reload` command.
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Don't stack a second server on the port.
$existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Already running on port 8000 (PID $($existing.OwningProcess))." -ForegroundColor Yellow
    Write-Host "Stop it first:  Get-NetTCPConnection -LocalPort 8000 -State Listen | " -NoNewline
    Write-Host "ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }"
    exit 1
}

# Activate the venv if one is present next to this script.
$venv = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $venv)) { $venv = Join-Path $PSScriptRoot "venv\Scripts\Activate.ps1" }
if (Test-Path $venv) { . $venv }

Write-Host "Starting InfraBeatOps on http://127.0.0.1:8000  (no reload)" -ForegroundColor Green
python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
