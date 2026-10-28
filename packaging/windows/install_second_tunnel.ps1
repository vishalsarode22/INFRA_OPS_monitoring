# A second SSH tunnel, to another machine that holds a client VPN (e.g. the
# AG group on 10.1.0.39). Runs next to "IBOPS Tunnel" (CEAT) and never
# touches it: own task, own forwards file, own logs.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\install_second_tunnel.ps1 -Name ag -Target ibops@10.1.0.39
#
# Task:      "IBOPS Tunnel AG"   (at log-on + every 5 minutes as a safety net)
# Forwards:  config\tunnel_forwards_ag.txt   (written by scripts\move_behind_tunnel.py --vm ag)
# Logs:      logs\supervisor-tunnel-ag.log, logs\tunnel_ssh_errors-ag.log
# Remove:    Unregister-ScheduledTask "IBOPS Tunnel AG" -Confirm:$false
param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Target)
$ErrorActionPreference = "Stop"
if ($Name -notmatch '^[a-z0-9]{1,12}$') { throw "Name: lower-case letters/digits only (e.g. ag)" }
if ($Target -notmatch '^[A-Za-z0-9._-]+@[0-9.]+$') { throw "Target: user@ip (e.g. ibops@10.1.0.39)" }
$here = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $here)
$key = Join-Path $env:USERPROFILE ".ssh\ibops_tunnel"
if (-not (Test-Path $key)) { throw "Tunnel key not found ($key)." }
$fwd = Join-Path $root "config\tunnel_forwards_$Name.txt"
if (-not (Test-Path $fwd)) { "# $Name tunnel forwards (local-ip:port:target-ip:port). Written by move_behind_tunnel.py --vm $Name`r`n" | Set-Content -Encoding ASCII $fwd }

$user = "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$task = "IBOPS Tunnel " + $Name.ToUpper()
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\ibops_tunnel_supervisor.ps1`" -Name $Name -Target $Target -ForwardsFile config\tunnel_forwards_$Name.txt"
Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $task -Action $action -Trigger @($trigger, $every5) -Settings $settings `
    -Principal $principal -Description "InfraBeatOps: SSH tunnel to $Target ($Name)" | Out-Null
Write-Host "installed: $task -> $Target (forwards: config\tunnel_forwards_$Name.txt)" -ForegroundColor Green
Write-Host "It starts once that file has forwards: run scripts\move_behind_tunnel.py --vm $Name next."
