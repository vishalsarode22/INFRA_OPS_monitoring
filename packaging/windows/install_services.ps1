# Install InfraBeatOps as Windows services with restart-on-failure.
# Run as Administrator from the project root:
#     powershell -ExecutionPolicy Bypass -File packaging\windows\install_services.ps1
#
# Uses NSSM (https://nssm.cc) -- put nssm.exe in packaging\windows\ or on PATH.
#
#   IBOPS-Web    services.web        -> Windows service  (survives logoff)
#   IBOPS-Jobs   services.jobs       -> Windows service  (survives logoff)
#   IBOPS-Collector                  -> NOT a service. It drives SAP GUI and
#                                       needs the interactive desktop, so it
#                                       is a Scheduled Task "at log on" with
#                                       restart-on-failure (see below).
param([string]$ServiceUser = "")   # e.g. ".\ibops" ; empty = LocalSystem

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$py   = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
$nssm = Join-Path $PSScriptRoot "nssm.exe"
if (-not (Test-Path $nssm)) { $nssm = (Get-Command nssm -ErrorAction Stop).Source }

function Install-IbopsService($name, $module, $role, $desc) {
    & $nssm stop $name 2>$null | Out-Null
    & $nssm remove $name confirm 2>$null | Out-Null
    & $nssm install $name $py "-m $module"
    & $nssm set $name AppDirectory $root
    & $nssm set $name AppEnvironmentExtra "IBO_ROLE=$role"
    & $nssm set $name Description $desc
    & $nssm set $name Start SERVICE_AUTO_START
    & $nssm set $name AppExit Default Restart          # restart on any exit
    & $nssm set $name AppRestartDelay 5000
    & $nssm set $name AppThrottle 10000
    & $nssm set $name AppStdout (Join-Path $root "logs\$name.out.log")
    & $nssm set $name AppStderr (Join-Path $root "logs\$name.err.log")
    & $nssm set $name AppRotateFiles 1
    & $nssm set $name AppRotateBytes 10485760
    if ($ServiceUser) { & $nssm set $name ObjectName $ServiceUser }
    & $nssm start $name
    Write-Host "installed and started $name" -ForegroundColor Green
}

New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Install-IbopsService "IBOPS-Web"  "services.web"  "ui"   "InfraBeatOps dashboard (UI only)"
Install-IbopsService "IBOPS-Jobs" "services.jobs" "jobs" "InfraBeatOps mail, AI and maintenance jobs"

# Collector: Scheduled Task in the logged-on session, restarts on failure.
$taskName = "IBOPS-Collector"
$action   = New-ScheduledTaskAction -Execute $py -Argument "-m services.collector" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Days 365) -MultipleInstances IgnoreNew -StartWhenAvailable
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "InfraBeatOps collector (SAP GUI sweeps) - runs in the logged-on desktop session" | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "registered scheduled task $taskName (at log on, restart every minute on failure)" -ForegroundColor Green
Write-Host "Keep the monitoring user logged on (auto-logon + screen never locks) or SAP GUI cannot run." -ForegroundColor Yellow
