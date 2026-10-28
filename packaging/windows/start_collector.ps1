# InfraBeatOps -- collector process (IBO_ROLE=collector).
# Runs the scheduler and the SAP GUI sweeps. Serves its own copy of the UI
# on port 8001 for the operator watching a sweep. Must run in the
# interactive desktop session that owns SAP GUI (not as a service).
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$env:IBO_ROLE = "collector"
if (Test-Path ".venv\Scripts\Activate.ps1") { . .venv\Scripts\Activate.ps1 }
Write-Host "InfraBeatOps collector on http://127.0.0.1:8001 (IBO_ROLE=collector)" -ForegroundColor Green
python -m dashboard.serve --port 8001   # binds IBO_BIND_HOST, default 127.0.0.1
