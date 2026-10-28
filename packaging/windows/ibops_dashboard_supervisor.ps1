# Keeps the Windows dashboard running. Started at log-on by the scheduled
# task "IBOPS Dashboard" (install_windows_autostart.ps1).
#   - frees the dashboard port (IBO_BIND_PORT, default 8000) from a leftover dashboard
#   - restarts the dashboard 10 s after it exits or crashes
#   - restarts it when /healthz has not answered for 3 minutes (hung)
# Runs in the logged-on desktop session on purpose: "Monitor this system"
# drives SAP GUI, which needs that desktop.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
$log = Join-Path $root "logs\supervisor-dashboard.log"
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Path $log }
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
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
$port = [int](Get-EnvSetting "IBO_BIND_PORT" "8000")
$https = [bool](Get-EnvSetting "IBO_TLS_CERTFILE" "")

# Health check with a hard time limit and no proxy lookup. Invoke-WebRequest
# on Windows PowerShell 5.1 can hang past -TimeoutSec (proxy auto-detection),
# which once left this supervisor stuck and the dashboard not restarted.
function Test-Dashboard {
    if ($https) {
        # The certificate is for ibops.infrabeat.net, not 127.0.0.1: check
        # with curl.exe (built into Windows 10/11) and skip the name check.
        try {
            $code = & curl.exe -k -s -o NUL -w "%{http_code}" --max-time 10 "https://127.0.0.1:$port/healthz"
            return ($code -eq "200")
        } catch {
            return $false
        }
    }
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$port/healthz")
        $req.Proxy = $null
        $req.Timeout = 10000
        $req.ReadWriteTimeout = 10000
        $resp = $req.GetResponse()
        $ok = ([int]$resp.StatusCode -eq 200)
        $resp.Close()
        return $ok
    } catch {
        return $false
    }
}

$mutex = New-Object System.Threading.Mutex($false, "Local\IBOPS-Dashboard-Supervisor")
if (-not $mutex.WaitOne(0)) { exit 0 }
Log "supervisor started (pid $PID), dashboard port $port$(if ($https) { ' (https)' })"

while ($true) {
  try {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -match 'uvicorn\s+dashboard\.app|run_dashboard\.py') {
            Log "freeing port ${port}: stopping leftover dashboard pid $($p.ProcessId)"
            & taskkill.exe /F /T /PID $p.ProcessId | Out-Null
        } elseif ($p) {
            Log "port $port is held by something else ($($p.Name) pid $($p.ProcessId)); not touching it"
        }
    }
    Start-Sleep 2
    $proc = Start-Process -FilePath $py `
        -ArgumentList ('"' + (Join-Path $root "run_dashboard.py") + '"') `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $root "logs\dashboard_stdout.log") `
        -RedirectStandardError  (Join-Path $root "logs\dashboard_stderr.log")
    Log "dashboard started, pid $($proc.Id)"
    Start-Sleep 30
    $fails = 0
    $beat = Get-Date
    while (-not $proc.HasExited) {
        if (Test-Dashboard) {
            $fails = 0
        } else {
            $fails++
            Log "health check failed ($fails of 3)"
        }
        if (((Get-Date) - $beat).TotalMinutes -ge 60) {
            Log "alive: dashboard pid $($proc.Id) running"
            $beat = Get-Date
        }
        if ($fails -ge 3) {
            Log "dashboard not answering for 3 minutes -- stopping pid $($proc.Id) and its children"
            & taskkill.exe /F /T /PID $proc.Id | Out-Null
            break
        }
        Start-Sleep 60
    }
    Log "dashboard stopped (exit code $($proc.ExitCode)); restarting in 10 s"
  } catch {
    Log "supervisor error (recovering): $($_.Exception.Message)"
  }
  Start-Sleep 10
}

