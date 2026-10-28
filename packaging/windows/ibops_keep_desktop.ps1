# InfraBeatOps -- keep the monitoring user's desktop alive for SAP GUI sweeps.
#
# Run by the scheduled task "IBOPS Keep Desktop" as SYSTEM (installed by
# install_keep_awake.ps1). Not meant to be run by hand.
#
# Closing the Remote Desktop window leaves the Windows session DISCONNECTED.
# Programs keep running (dashboard, tunnel), but a disconnected session has
# no screen: SAP GUI cannot log on by scripting reliably and its HardCopy
# screenshots come out black. This moves the monitoring user's disconnected
# session to the console (tscon ... /dest:console) -- the session stays
# logged on, with a screen, exactly as it was. It is the standard way to
# leave a UI-automation VM.
#
# It only ever touches the ONE user named in -User (the monitoring account),
# and only when that user's session is disconnected. Other users' sessions
# are never touched. Nothing is logged off, nothing is closed.
param([Parameter(Mandatory = $true)][string]$User,
      [string]$LogFile = "$env:ProgramData\InfraBeatOps\keep-desktop.log")
$ErrorActionPreference = "Continue"
$log = $LogFile
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Add-Content -Path $log }

$name = ($User -split "\\")[-1]
# qwinsta columns: SESSIONNAME USERNAME ID STATE TYPE DEVICE. A disconnected
# session has no SESSIONNAME, so match on "<user> <id> Disc".
$lines = & qwinsta 2>$null
foreach ($line in $lines) {
    if ($line -match "(?i)\s$([regex]::Escape($name))\s+(\d+)\s+(Disc|Disconnected)\b") {
        $id = $Matches[1]
        & tscon.exe $id /dest:console 2>&1 | ForEach-Object { Log "tscon: $_" }
        if ($LASTEXITCODE -eq 0) {
            Log "session $id of ${name} was disconnected -> moved to the console (desktop kept alive for SAP GUI)"
        } else {
            Log "session $id of ${name}: tscon to console failed (exit $LASTEXITCODE)"
        }
    }
}
