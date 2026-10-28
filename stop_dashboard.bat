@echo off
rem Stop the IBOPS dashboard safely: first let a running sweep finish (up to
rem 20 minutes; no new sweep starts meanwhile), then stop it on the port set in
rem .env (IBO_BIND_PORT, default 8000) and its redirect port. Only python
rem processes are stopped.
rem   stop_dashboard.bat          wait for a running sweep, then stop
rem   stop_dashboard.bat now      stop at once (a running sweep is cut off)
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$rt = if ($env:IBO_RUNTIME_DIR) { $env:IBO_RUNTIME_DIR } else { 'runtime' }; New-Item -ItemType Directory -Force -Path $rt | Out-Null;" ^
  "$flag = Join-Path $rt 'stop_pending'; $active = Join-Path $rt 'active_run.json';" ^
  "if ('%~1' -ne 'now') { Set-Content -Path $flag -Value (Get-Date -Format s);" ^
  "  $until = (Get-Date).AddMinutes(20);" ^
  "  while ((Test-Path $active) -and (Get-Date) -lt $until) { try { $a = Get-Content $active -Raw | ConvertFrom-Json; Write-Output ('Waiting: a sweep of ' + $a.system + ' is running (started ' + $a.started_at + ') ...') } catch { Write-Output 'Waiting for the running sweep ...' }; Start-Sleep 15 };" ^
  "  if (Test-Path $active) { Write-Output 'The sweep is still running after 20 minutes; stopping anyway (it is recorded as interrupted and runs again after the start).' } };" ^
  "$ports=@(8000); if (Test-Path .env) { foreach ($l in Get-Content .env) { if ($l -match '^\s*IBO_(BIND|HTTP_REDIRECT)_PORT\s*=\s*(\d+)') { $ports += [int]$Matches[2] } } };" ^
  "$pids = Get-NetTCPConnection -State Listen -LocalPort ($ports | Select-Object -Unique) -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique;" ^
  "foreach ($p in $pids) { $pr = Get-Process -Id $p -ErrorAction SilentlyContinue; if (-not $pr) { continue }; if ($pr.ProcessName -like 'python*') { Stop-Process -Id $p -Force; Write-Output ('stopped ' + $pr.ProcessName + ' PID ' + $p) } else { Write-Output ('left alone: ' + $pr.ProcessName + ' PID ' + $p + ' (not the dashboard)') } };" ^
  "Remove-Item $flag -ErrorAction SilentlyContinue; Start-Sleep 3"
echo Dashboard stopped.
exit /b 0
