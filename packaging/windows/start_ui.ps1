# InfraBeatOps -- UI process (IBO_ROLE=ui).
# Serves the wall and live RFC reads on port 8000. Never sweeps, never
# drives SAP GUI, so a GUI hang in the collector cannot freeze the wall.
Set-Location (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$env:IBO_ROLE = "ui"
if (Test-Path ".venv\Scripts\Activate.ps1") { . .venv\Scripts\Activate.ps1 }
Write-Host "InfraBeatOps UI on http://127.0.0.1:8000 (IBO_ROLE=ui, no sweeps)" -ForegroundColor Green
python -m dashboard.serve --port 8000   # binds IBO_BIND_HOST, default 127.0.0.1
