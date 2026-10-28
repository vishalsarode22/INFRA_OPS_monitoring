$nssm = Join-Path $PSScriptRoot "nssm.exe"; if (-not (Test-Path $nssm)) { $nssm = (Get-Command nssm).Source }
foreach ($n in "IBOPS-Web","IBOPS-Jobs") { & $nssm stop $n; & $nssm remove $n confirm }
Stop-ScheduledTask -TaskName "IBOPS-Collector" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "IBOPS-Collector" -Confirm:$false -ErrorAction SilentlyContinue
