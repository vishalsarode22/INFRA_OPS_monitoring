# Start the dashboard in the background (no console window).
# Runs from this script's own folder with its .venv -- no hard-coded drive
# or venv name. Binds to 127.0.0.1 unless IBO_BIND_HOST is set (in .env or
# the environment); see dashboard/serve.py.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$root = $Root
# Port and HTTPS come from .env (IBO_BIND_PORT, IBO_TLS_CERTFILE), the same
# settings dashboard/serve.py uses, so the dashboard can serve the company
# address (e.g. http://ibops.infrabeat.net on port 80) without editing this file.
function Get-EnvSetting([string]$Name, [string]$Default) {
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match "^\s*$Name\s*=\s*(.*)$") {
                $v = $Matches[1].Trim().Trim('"').Trim("'")
                if ($v) { return $v }
            }
        }
    }
    return $Default
}
$Port = [int](Get-EnvSetting "IBO_BIND_PORT" "8000")

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Dashboard already running on port $Port."
    exit
}

$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Output "Python virtual environment not found at $Root\.venv -- create it first (python -m venv .venv)."
    exit 1
}
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Start-Process -RedirectStandardOutput/-Error OVERWRITE their files. Keep the
# previous run's output by rotating it to <name>.1 first (one generation).
foreach ($name in @("dashboard_stdout.log", "dashboard_stderr.log")) {
    $f = Join-Path $logDir $name
    if (Test-Path $f) {
        try {
            Move-Item -Force -Path $f -Destination "$f.1"
        } catch {
            Write-Output "Note: could not rotate $name ($($_.Exception.Message)); it will be overwritten."
        }
    }
}

Start-Process -WindowStyle Hidden -FilePath $py `
    -ArgumentList @("-m", "dashboard.serve", "--port", "$Port") `
    -WorkingDirectory $Root `
    -RedirectStandardOutput (Join-Path $logDir "dashboard_stdout.log") `
    -RedirectStandardError (Join-Path $logDir "dashboard_stderr.log")

Start-Sleep -Seconds 5

$check = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($check) {
    Write-Output "Dashboard started successfully on port $Port."
} else {
    Write-Output "WARNING: Dashboard did not bind to port $Port. Check logs\dashboard_stderr.log for errors."
}
