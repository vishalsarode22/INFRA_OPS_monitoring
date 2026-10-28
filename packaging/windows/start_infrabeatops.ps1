$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $Root "venv\Scripts\python.exe" }
if (-not (Test-Path $py)) {
    throw "Python virtual environment not found at $Root\.venv"
}
# Binds to 127.0.0.1 unless IBO_BIND_HOST (in .env or the environment) says
# otherwise -- see dashboard/serve.py and packaging/OPERATIONS.md.
& $py -m dashboard.serve --port 8000
