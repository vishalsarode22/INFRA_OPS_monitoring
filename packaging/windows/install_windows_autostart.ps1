# Start the Windows dashboard and the tunnel automatically at log-on, and
# keep them running. Run once, in the project folder:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\install_windows_autostart.ps1
# Two scheduled tasks for the current user ("At log on"):
#   IBOPS Dashboard -> ibops_dashboard_supervisor.ps1
#   IBOPS Tunnel    -> ibops_tunnel_supervisor.ps1   (needs the tunnel key first)
# Remove with: Unregister-ScheduledTask "IBOPS Dashboard","IBOPS Tunnel" -Confirm:$false
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$user = "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
# Safety net: also start every 5 minutes. If the supervisor is running, the
# new start does nothing (MultipleInstances IgnoreNew + a mutex in the
# script); if it died for any reason, it is back within 5 minutes.
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)

function Install-Task($name, $script) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\$script`""
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $name -Action $action -Trigger @($trigger, $every5) -Settings $settings `
        -Principal $principal -Description "InfraBeatOps: keeps $script running" | Out-Null
    Start-ScheduledTask -TaskName $name
    Write-Host "installed and started: $name" -ForegroundColor Green
}

if (-not (Test-Path "$env:USERPROFILE\.ssh\ibops_tunnel")) {
    Write-Host "Tunnel key not found ($env:USERPROFILE\.ssh\ibops_tunnel). Set it up first (see the steps), then run this again." -ForegroundColor Yellow
    exit 1
}
Install-Task "IBOPS Dashboard" "ibops_dashboard_supervisor.ps1"
Install-Task "IBOPS Tunnel"    "ibops_tunnel_supervisor.ps1"
Write-Host "Logs: logs\supervisor-dashboard.log, logs\supervisor-tunnel.log"
