# Keeps the SSH tunnel to the Ubuntu collector open. Started at log-on by
# the scheduled task "IBOPS Tunnel" (install_windows_autostart.ps1).
# Uses the key-only login set up for the tunnel (no password prompt); the
# key is allowed to forward to the Ubuntu dashboard and nothing else.
# A second tunnel to another machine (e.g. the AG VM) runs as its own task:
#   ibops_tunnel_supervisor.ps1 -Name ag -Target ibops@10.1.0.39 -ForwardsFile config\tunnel_forwards_ag.txt
# It has its own lock, logs and forwards file; only the main tunnel carries
# the dashboard forward (8001).
param([string]$Name = "", [string]$Target = "", [string]$ForwardsFile = "")
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$sfx = if ($Name) { "-$Name" } else { "" }
$log = Join-Path $root "logs\supervisor-tunnel$sfx.log"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
function Log($m) {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Move-Item -Force $log "$log.1" }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Path $log
}
$key  = Join-Path $env:USERPROFILE ".ssh\ibops_tunnel"
$hostSpec = if ($Target) { $Target } else { $env:IBOPS_TUNNEL_TARGET }; if (-not $hostSpec) { $hostSpec = "ibops@10.1.0.37" }

# Extra forwards, one per line in config\tunnel_forwards.txt, for SAP systems
# reached through the collector's VPN, e.g.
#   127.0.0.2:3200:192.0.2.127:3200     # SARLOHA_QAS  SAP GUI
#   127.0.0.2:3300:192.0.2.127:3300     # SARLOHA_QAS  RFC
# The Ubuntu side must allow each target (packaging/ubuntu/allow_tunnel_targets.sh).
$fwdFile = if ($ForwardsFile) { Join-Path $root $ForwardsFile } else { Join-Path $root "config\tunnel_forwards.txt" }
# Read again before EVERY connect (28.10.2026: a list read once, while it
# still held a broken line, kept the CEAT tunnel down after the file was fixed).
# A line that is not local-ip:port:target:port with a real host/IP target
# (e.g. an unresolved ${VAR}) is skipped and logged -- it cannot take the
# whole tunnel down any more.
function Read-Forwards {
    $f = @()
    if (-not $Name) { $f += @("-L", "8001:127.0.0.1:8000") }
    if (Test-Path $fwdFile) {
        foreach ($line in Get-Content $fwdFile) {
            $l = ($line -replace "#.*$", "").Trim()
            if ($l -match '^\d+\.\d+\.\d+\.\d+:\d+:[A-Za-z0-9][A-Za-z0-9.\-]*:\d+$') { $f += @("-L", $l) }
            elseif ($l) { Log "ignored line in tunnel_forwards.txt: $l" }
        }
    }
    return ,$f
}

# Only one supervisor at a time (the task now also re-starts every 5 minutes
# as a safety net; a second copy must exit, not fight the first).
$mutex = New-Object System.Threading.Mutex($false, "Local\IBOPS-Tunnel-Supervisor$sfx")
if (-not $mutex.WaitOne(0)) { exit 0 }
Log "supervisor started (pid $PID)"

while ($true) {
  try {
    $forwards = Read-Forwards
    # An old tunnel window holding 8001 would make every new tunnel fail.
    if (-not $Name) { Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        if ($p -and $p.ProcessName -eq "ssh") {
            Log "freeing port 8001: stopping old ssh pid $($p.Id)"
            Stop-Process -Id $p.Id -Force
        }
    } }
    # Old ssh windows holding any of the forwarded local ports would make the
    # new tunnel fail (ExitOnForwardFailure); free those too.
    if (-not $forwards.Count) { Log "no forwards in $fwdFile -- waiting"; Start-Sleep 45; continue }
    foreach ($i in 0..($forwards.Count - 1)) {
        if ($forwards[$i] -ne "-L") { continue }
        $parts = $forwards[$i + 1].Split(":")
        $lport = if ($parts.Count -eq 4) { [int]$parts[1] } else { [int]$parts[0] }
        $laddr = if ($parts.Count -eq 4) { $parts[0] } else { "127.0.0.1" }
        Get-NetTCPConnection -LocalAddress $laddr -LocalPort $lport -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
            $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -eq "ssh") { Log "freeing ${laddr}:${lport}: stopping old ssh pid $($p.Id)"; Stop-Process -Id $p.Id -Force }
        }
    }
    Log "starting tunnel to $hostSpec ($([int]($forwards.Count / 2)) forward(s))"
    $sshArgs = @("-N", "-i", "`"$key`"", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
                 "-o", "ConnectTimeout=15") + $forwards + @($hostSpec)
    # Start-Process instead of "& ssh.exe ... 2>>": in Windows PowerShell 5.1
    # redirected stderr from a native program turns into PowerShell errors,
    # which is what kept showing up as NativeCommandError in the log.
    $p = Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -NoNewWindow -PassThru `
         -RedirectStandardError (Join-Path $root "logs\tunnel_ssh_last_error$sfx.log")
    $p.WaitForExit()
    $err = Get-Content (Join-Path $root "logs\tunnel_ssh_last_error$sfx.log") -ErrorAction SilentlyContinue
    $errLog = Join-Path $root "logs\tunnel_ssh_errors$sfx.log"
    # Rotate at 5 MB (it grew to 67 MB while a host was unreachable for weeks).
    if ((Test-Path $errLog) -and ((Get-Item $errLog).Length -gt 5MB)) { Move-Item -Force $errLog "$errLog.1" }
    if ($err) { $err | Add-Content $errLog }
    Log "tunnel ended (exit code $($p.ExitCode)); reconnecting in 15 s"
  } catch {
    # Nothing may end this loop: log and go round again.
    Log "supervisor error (recovering): $($_.Exception.Message)"
  }
  Start-Sleep 15
}
