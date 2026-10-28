# InfraBeatOps -- the Windows monitoring VM never sleeps, never locks itself,
# and keeps a desktop for SAP GUI sweeps when you close Remote Desktop.
#
# Run it logged on as the monitoring user (the account the IBOPS Dashboard
# task runs as):
#   cd $env:USERPROFILE\ibops\infrabeatops
#   powershell -ExecutionPolicy Bypass -File packaging\windows\install_keep_awake.ps1
# - In a NORMAL PowerShell (no administrator): does step 1 (power) and step 2
#   (screen saver lock) for this account, then lists what still needs an
#   administrator.
# - In an ELEVATED PowerShell (an administrator, e.g. IT), pass the monitoring
#   account:  ... install_keep_awake.ps1 -User "DESKTOP-UIHP42K\Girish_BasisTestVM"
#   and it does everything, including step 3.
# Undo:   ... install_keep_awake.ps1 -Undo
#
# What it does:
#   1. Power: never sleep, never hibernate, display never off (plugged in and
#      on battery), no sign-in required after wake.
#   2. No automatic screen lock for this user: screen saver off, and no
#      "machine inactivity limit" lock.
#   3. Task "IBOPS Keep Desktop" (runs as SYSTEM): when THIS user's Remote
#      Desktop session is disconnected (you closed the RDP window), the session
#      is moved to the console instead, so it keeps its screen. Runs on the
#      disconnect event and every 5 minutes as a safety net.
#      Only this user's session is ever touched.
#
# A sleeping or switched-off VM runs nothing -- no program can monitor from a
# sleeping machine. That is why sleep is switched off here.
#
# Leaving the VM: just close the Remote Desktop window. Do NOT press Win+L
# (lock) and do NOT sign out: a locked screen can give black screenshots, and
# signing out stops the dashboard and the tunnel until the next log-on.
param([switch]$Undo, [string]$User = "$env:USERDOMAIN\$env:USERNAME")
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Not running as administrator: doing what this account may change itself" -ForegroundColor Yellow
    Write-Host "(power timeouts, screen saver lock). The parts that need an administrator are listed at the end." -ForegroundColor Yellow
}
$here = $PSScriptRoot
$taskName = "IBOPS Keep Desktop"

if ($Undo) {
    if ($isAdmin) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue }
    powercfg /change standby-timeout-ac 30 | Out-Null
    powercfg /change monitor-timeout-ac 15 | Out-Null
    if ($isAdmin) { Write-Host "Removed '$taskName'." -ForegroundColor Yellow }
    else { Write-Host "'$taskName' (if installed) was left in place: removing it needs an administrator." -ForegroundColor Yellow }
    Write-Host "Sleep after 30 min and display off after 15 min again (plugged in)." -ForegroundColor Yellow
    Write-Host "Screen saver settings were left as they are now." -ForegroundColor Yellow
    exit 0
}

Write-Host "== 1. power: never sleep / hibernate / display off" -ForegroundColor Cyan
$ErrorActionPreference = "Continue"     # powercfg writes to stderr on some editions; not fatal
foreach ($s in "standby-timeout-ac", "standby-timeout-dc", "hibernate-timeout-ac", "hibernate-timeout-dc",
                "monitor-timeout-ac", "monitor-timeout-dc") {
    powercfg /change $s 0 | Out-Null
}
if ($isAdmin) { powercfg /hibernate off 2>$null | Out-Null }
# "Require a password on wakeup" off (GUID of the CONSOLELOCK setting).
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null | Out-Null
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null | Out-Null
powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
$sleepAc = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null | Select-String "Current AC Power Setting Index" | Select-Object -First 1)
if ($sleepAc -and "$sleepAc" -match "0x0+\s*$") {
    Write-Host "   sleep: never (checked)" -ForegroundColor Green
} else {
    Write-Host "   sleep setting could not be confirmed as 'never' ($sleepAc) -- an administrator or a company policy may block it" -ForegroundColor Yellow
}
$ErrorActionPreference = "Stop"

Write-Host "== 2. no automatic screen lock" -ForegroundColor Cyan
$desk = "HKCU:\Control Panel\Desktop"
Set-ItemProperty $desk -Name ScreenSaveActive -Value "0"
Set-ItemProperty $desk -Name ScreenSaverIsSecure -Value "0"
$pol = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
$cur = (Get-ItemProperty $pol -Name InactivityTimeoutSecs -ErrorAction SilentlyContinue).InactivityTimeoutSecs
if ($cur -and -not $isAdmin) {
    Write-Host "   machine inactivity lock is $cur s -- needs an administrator to switch off" -ForegroundColor Yellow
} elseif ($cur) {
    Set-ItemProperty $pol -Name InactivityTimeoutSecs -Value 0 -Type DWord
    Write-Host "   machine inactivity lock was $cur s -> off"
}
Write-Host "   screen saver and its lock off for $env:USERDOMAIN\$env:USERNAME (the account running this script)"
Write-Host "   (If a company Group Policy forces a screen lock it will come back -- ask IT to exclude this VM.)" -ForegroundColor Yellow

if (-not $isAdmin) {
    Write-Host ""
    Write-Host "Done for this account. Still needs an ADMINISTRATOR of this VM (ask IT to run, once, in an" -ForegroundColor Yellow
    Write-Host "elevated PowerShell:  powershell -ExecutionPolicy Bypass -File $PSCommandPath -User `"$User`")" -ForegroundColor Yellow
    Write-Host "   - the 'IBOPS Keep Desktop' task (keeps the screen when Remote Desktop is closed)" -ForegroundColor Yellow
    Write-Host "   - hibernate off, and the machine inactivity lock if one is set" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Until then: leave the VM by MINIMISING the Remote Desktop window, not closing it, and do not" -ForegroundColor Green
    Write-Host "press Win+L or sign out. (Minimised needs one setting on YOUR laptop -- see the instructions.)" -ForegroundColor Green
    exit 0
}

Write-Host "== 3. task '$taskName' (SYSTEM): keep this user's desktop when RDP is closed" -ForegroundColor Cyan
$src = Join-Path $here "ibops_keep_desktop.ps1"
if (-not (Test-Path $src)) { throw "missing $src" }
# The task runs as SYSTEM, so its script must live where only Administrators
# and SYSTEM can change it -- NOT in a user's profile (anyone who could edit
# it there would get SYSTEM rights every 5 minutes).
$dir = Join-Path $env:ProgramData "InfraBeatOps"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
& icacls $dir /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not set permissions on $dir" }
$script = Join-Path $dir "ibops_keep_desktop.ps1"
Copy-Item $src $script -Force
$logFile = Join-Path $dir "keep-desktop.log"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -User `"$User`" -LogFile `"$logFile`""
# Event 24 = "Remote Desktop session has been disconnected" (LocalSessionManager log).
$cls = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$onDisconnect = New-CimInstance -CimClass $cls -ClientOnly
$onDisconnect.Enabled = $true
$onDisconnect.Subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"><Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[(EventID=24)]]</Select></Query></QueryList>'
$every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$atStart = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
$sys = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($onDisconnect, $every5, $atStart) `
    -Settings $settings -Principal $sys -Description "InfraBeatOps: moves $User's disconnected RDP session to the console so SAP GUI sweeps keep a screen" | Out-Null
Write-Host "   installed (script and log in $dir, writable only by Administrators and SYSTEM)" -ForegroundColor Green

Write-Host ""
Write-Host "Done. From now on leave this VM by CLOSING the Remote Desktop window (X)." -ForegroundColor Green
Write-Host "Do not press Win+L and do not sign out." -ForegroundColor Green
