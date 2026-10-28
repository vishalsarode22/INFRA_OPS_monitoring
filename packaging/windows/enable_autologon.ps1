# InfraBeatOps -- automatic log-on for the Windows monitoring VM.
#
# Why: SAP GUI screenshot sweeps need a logged-on desktop, and the IBOPS
# Dashboard / IBOPS Tunnel tasks start "at log on". After a reboot (Windows
# Update, host maintenance) nothing runs until someone logs on by hand. With
# automatic log-on the VM comes back on its own.
#
# How: Sysinternals Autologon (Microsoft). It stores the password as an
# encrypted LSA secret -- NOT in plain text in the registry, which is what
# the "DefaultPassword" registry method does. This script never writes the
# password to a file and never prints it; it is handed to Autologon on its
# command line for the moment the tool runs (Autologon offers no other way),
# so run it on the console, not over a session that logs process commands.
#
# Run in an ELEVATED PowerShell ON THE WINDOWS VM:
#   cd $env:USERPROFILE\ibops\infrabeatops
#   powershell -ExecutionPolicy Bypass -File packaging\windows\enable_autologon.ps1
# To switch it off again:
#   powershell -ExecutionPolicy Bypass -File packaging\windows\enable_autologon.ps1 -Disable
#
# Security note: anyone who can power-cycle this VM gets a logged-on desktop.
# Keep the VM's console access (hypervisor) restricted, and consider -Lock,
# which locks the screen 2 minutes after log-on. (Test a sweep with the
# screen locked first: some SAP GUI versions take blank screenshots while
# the session is locked.)
param(
    [switch]$Disable,
    [switch]$Lock,
    [string]$User = $env:USERNAME,
    [string]$Domain = $env:USERDOMAIN
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an elevated PowerShell (Run as administrator)."
}

$tools = Join-Path $PSScriptRoot "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null
$exe = Join-Path $tools "Autologon64.exe"
if (-not (Test-Path $exe)) {
    Write-Host "Downloading Sysinternals Autologon from live.sysinternals.com ..." -ForegroundColor Cyan
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing "https://live.sysinternals.com/Autologon64.exe" -OutFile $exe
    } catch {
        throw "Could not download Autologon64.exe ($($_.Exception.Message)). Download it by hand from " +
              "https://learn.microsoft.com/sysinternals/downloads/autologon and put it in $tools"
    }
}
$sig = Get-AuthenticodeSignature $exe
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Microsoft") {
    Remove-Item $exe -Force
    throw "Autologon64.exe is not signed by Microsoft (status $($sig.Status)) -- removed it. Download it by hand."
}

if ($Disable) {
    # Autologon with an empty password turns it off and deletes the stored secret.
    Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" -Name AutoAdminLogon -Value "0"
    & $exe /accepteula $User $Domain "" | Out-Null
    Unregister-ScheduledTask -TaskName "IBOPS Lock After Logon" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Automatic log-on is OFF." -ForegroundColor Green
    exit 0
}

Write-Host "Automatic log-on for $Domain\$User" -ForegroundColor Cyan
$sec = Read-Host -AsSecureString "Windows password for $Domain\$User (not shown, not saved by this script)"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if (-not $plain) { throw "Empty password -- nothing changed." }
    $p = Start-Process -FilePath $exe -ArgumentList @("/accepteula", $User, $Domain, $plain) -Wait -PassThru -WindowStyle Hidden
    if ($p.ExitCode -ne 0) { throw "Autologon exited with code $($p.ExitCode)." }
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $plain = $null
}

if ($Lock) {
    $action = New-ScheduledTaskAction -Execute "rundll32.exe" -Argument "user32.dll,LockWorkStation"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$Domain\$User"
    $trigger.Delay = "PT2M"
    Register-ScheduledTask -TaskName "IBOPS Lock After Logon" -Action $action -Trigger $trigger `
        -Description "Locks the screen 2 minutes after the automatic log-on (InfraBeatOps)" -Force | Out-Null
    Write-Host "The screen will lock 2 minutes after each automatic log-on." -ForegroundColor Yellow
}

# The two IBOPS tasks must start at log-on (install_windows_autostart.ps1).
foreach ($t in @("IBOPS Dashboard", "IBOPS Tunnel")) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) { Write-Host "  task '$t': $($task.State)" } else {
        Write-Host "  task '$t' is MISSING -- run packaging\windows\install_windows_autostart.ps1" -ForegroundColor Red }
}
Write-Host ""
Write-Host "Automatic log-on is ON. Test it: restart the VM and check that the dashboard comes back" -ForegroundColor Green
Write-Host "(http://127.0.0.1:8000/health) without anyone logging on."
