<#
IBOPS VPN reconnect for the FREE FortiClient VPN (no FortiSSLVPNclient.exe needed).

Every run (Task Scheduler, every 2 minutes):
  1. Test the SAP host behind the VPN (TCP, 3 s). Answers -> done.
  2. Not answering twice in a row -> open the FortiClient window and press its
     own "Connect" button through Windows UI Automation (the same thing a
     screen reader does). The password comes from AG_VPN_PASSWORD in IBOPS's
     .env when FortiClient has not saved it. Nothing is typed with the
     keyboard and no other window is touched.
  3. Wait up to 60 s for the SAP host. Retries after 2, 5, 15 minutes, then
     STOPS (so a wrong password does not lock the account) until the VPN is
     back or you delete runtime\vpn_reconnect_state.json.

Needs: a logged-on Windows session on the VM (it can be a disconnected RDP
session, but not signed out). No admin rights.

  Check what it sees, change nothing:
    powershell -ExecutionPolicy Bypass -File vpn_reconnect.ps1 -Inspect
  One run now (shows what it does):
    powershell -ExecutionPolicy Bypass -File vpn_reconnect.ps1 -Verbose
  Another VPN on another PC (e.g. CEAT), password in vpn.env next to this script:
    powershell -ExecutionPolicy Bypass -File vpn_reconnect.ps1 -Name CEAT -Probe <ceat-host>:33NN -PasswordVar CEAT_VPN_PASSWORD -Inspect
#>
[CmdletBinding()]
param(
    [string]$Name = "AG",                          # label: separate log and state per VPN
    [string]$Probe = "198.51.100.31:3300",        # an SAP host:port behind the VPN (default: PS4 behind AG)
    [string]$ProjectDir = "C:\Users\Girish_BasisTestVM\ibops\infrabeatops",
    [string]$PasswordVar = "AG_VPN_PASSWORD",      # setting holding the VPN password (in the .env file)
    [string]$EnvFile = "",                         # empty: <ProjectDir>\.env, else vpn.env next to this script
    [string]$VpnName = "",                         # FortiClient connection name; empty = the one shown
    [switch]$Inspect,
    [switch]$Force                                 # ignore the back-off once
)
$ErrorActionPreference = 'Stop'
# On a machine without IBOPS (e.g. the CEAT VPN PC) everything stays next to this script.
if (-not (Test-Path $ProjectDir)) { $ProjectDir = $PSScriptRoot }
if (-not $EnvFile) {
    $EnvFile = Join-Path $ProjectDir '.env'
    if (-not (Test-Path $EnvFile)) { $EnvFile = Join-Path $PSScriptRoot 'vpn.env' }
}
$runtime = Join-Path $ProjectDir 'runtime'
if (-not (Test-Path $runtime)) { New-Item -ItemType Directory -Path $runtime | Out-Null }
$logFile = Join-Path $runtime "vpn_reconnect_$Name.log"
$stateFile = Join-Path $runtime "vpn_reconnect_state_$Name.json"

function Log([string]$m) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Verbose $line
    if ($Inspect) { Write-Host $line }
}

function Test-Probe([string]$hp, [int]$ms = 3000) {
    $h, $p = $hp.Split(':')
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $t = $c.ConnectAsync($h, [int]$p)
        return ($t.Wait($ms) -and $c.Connected)
    } catch { return $false } finally { $c.Close() }
}

function Get-State {
    if (Test-Path $stateFile) {
        try { return Get-Content $stateFile -Raw | ConvertFrom-Json } catch { }
    }
    return [pscustomobject]@{ fails = 0; attempts = 0; next = '2000-01-01 00:00:00'; stopped = $false }
}
function Save-State($s) { $s | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8 }

function Get-EnvValue([string]$name) {
    if (-not (Test-Path $EnvFile)) { return '' }
    foreach ($l in Get-Content $EnvFile) {
        if ($l -match "^\s*$name\s*=\s*(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return ''
}

# ------------------------------------------------------------ UI Automation
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$UIA = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]

function Get-FortiWindow {
    $proc = Get-Process -Name 'FortiClient' -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if (-not $proc) {
        # Minimised to the tray: starting the console again shows its window.
        $exe = @("$env:ProgramFiles\Fortinet\FortiClient\FortiClient.exe",
                 "${env:ProgramFiles(x86)}\Fortinet\FortiClient\FortiClient.exe") |
               Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $exe) { Log "FortiClient.exe not found"; return $null }
        Start-Process -FilePath $exe | Out-Null
        for ($i = 0; $i -lt 20 -and -not $proc; $i++) {
            Start-Sleep -Seconds 1
            $proc = Get-Process -Name 'FortiClient' -ErrorAction SilentlyContinue |
                    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
        }
    }
    if (-not $proc) { Log "FortiClient window did not open"; return $null }
    return $UIA::FromHandle($proc.MainWindowHandle)
}

function Find-All($root, $type) {
    $cond = New-Object System.Windows.Automation.PropertyCondition($UIA::ControlTypeProperty, $type)
    return $root.FindAll($TS::Descendants, $cond)
}
function Find-Button($root, [string[]]$names) {
    foreach ($b in (Find-All $root $CT::Button)) {
        $n = ($b.Current.Name + '').Trim()
        if ($names | Where-Object { $n -ieq $_ }) { return $b }
    }
    return $null
}
function Invoke-El($el) {
    $p = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); return $true }
    return $false
}

if ($Inspect) {
    Log ("probe {0}: {1}" -f $Probe, $(if (Test-Probe $Probe) { 'answers (VPN up)' } else { 'no answer (VPN down?)' }))
    $w = Get-FortiWindow
    if (-not $w) { exit 1 }
    Write-Host "FortiClient window: $($w.Current.Name)"
    foreach ($t in @($CT::Button, $CT::Edit, $CT::ComboBox, $CT::Text)) {
        foreach ($e in (Find-All $w $t)) {
            $n = ($e.Current.Name + '').Trim()
            if ($n -or $t -eq $CT::Edit) {
                Write-Host ("  {0,-9} name='{1}' password={2}" -f $t.ProgrammaticName.Replace('ControlType.', ''),
                            $n, $e.Current.IsPassword)
            }
        }
    }
    Write-Host "Nothing was changed. Send this list to Claude if Connect is not found."
    exit 0
}

# ------------------------------------------------------------ one check
$state = Get-State
if (Test-Probe $Probe) {
    if ($state.fails -or $state.attempts -or $state.stopped) { Log "VPN up again" }
    Save-State ([pscustomobject]@{ fails = 0; attempts = 0; next = '2000-01-01 00:00:00'; stopped = $false })
    exit 0
}
$state.fails = [int]$state.fails + 1
if ($state.fails -lt 2) { Save-State $state; Log "probe $Probe no answer (1st time) -- checking again next run"; exit 0 }
if ($state.stopped -and -not $Force) { Save-State $state; exit 0 }
if (-not $Force -and (Get-Date) -lt [datetime]$state.next) { Save-State $state; exit 0 }

Log "probe $Probe no answer twice -- reconnecting FortiClient"
$ok = $false
try {
    $w = Get-FortiWindow
    if ($w) {
        $connect = Find-Button $w @('Connect')
        if (-not $connect) {
            # Shown as connected but no traffic: disconnect first.
            $disc = Find-Button $w @('Disconnect')
            if ($disc) { Log "FortiClient says connected but nothing answers -- Disconnect"; Invoke-El $disc | Out-Null; Start-Sleep 5 }
            $connect = Find-Button $w @('Connect')
        }
        if ($VpnName) {
            foreach ($cb in (Find-All $w $CT::ComboBox)) {
                $v = $null
                if ($cb.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$v) -and
                    $v.Current.Value -and $v.Current.Value -ne $VpnName) {
                    Log "VPN name shown is '$($v.Current.Value)', expected '$VpnName' -- not changing it"
                }
            }
        }
        $pw = Find-All $w $CT::Edit | Where-Object { $_.Current.IsPassword } | Select-Object -First 1
        if ($pw) {
            $secret = Get-EnvValue $PasswordVar
            $vp = $null
            if ($secret -and $pw.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
                $vp.SetValue($secret)
                Log "password filled from $EnvFile ($PasswordVar)"
            } elseif (-not $secret) {
                Log "password box is empty and $PasswordVar is not in $EnvFile"
            }
            $secret = $null
        }
        if ($connect -and (Invoke-El $connect)) {
            Log "Connect pressed"
            for ($i = 0; $i -lt 20; $i++) {
                Start-Sleep -Seconds 3
                if (Test-Probe $Probe) { $ok = $true; break }
            }
        } else {
            Log "Connect button not found in the FortiClient window (run with -Inspect)"
        }
    }
} catch {
    Log ("reconnect error: {0}" -f $_.Exception.Message)
}

if ($ok) {
    Log "reconnected -- $Probe answers"
    Save-State ([pscustomobject]@{ fails = 0; attempts = 0; next = '2000-01-01 00:00:00'; stopped = $false })
    exit 0
}
$state.attempts = [int]$state.attempts + 1
$delays = @(2, 5, 15)
if ($state.attempts -gt $delays.Count) {
    $state.stopped = $true
    Log "STOPPED after $($state.attempts) failed tries (protects the VPN account). Reconnect by hand; it resumes by itself once the VPN is up."
} else {
    $d = $delays[$state.attempts - 1]
    $state.next = (Get-Date).AddMinutes($d).ToString('yyyy-MM-dd HH:mm:ss')
    Log "try $($state.attempts) failed -- next try in $d min"
}
Save-State $state
exit 1
