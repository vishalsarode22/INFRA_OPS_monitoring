$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
if (-not (Test-Path "$Root\venv\Scripts\python.exe")) {
    throw "Python virtual environment not found at $Root\venv"
}
& "$Root\venv\Scripts\python.exe" -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
