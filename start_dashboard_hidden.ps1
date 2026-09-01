$existing = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "Dashboard already running on port 8000."
    exit
}

Start-Process -WindowStyle Hidden -FilePath "cmd.exe" `
    -ArgumentList "/c cd /d D:\SAP_BASIS_MONITOR && venv\Scripts\activate.bat && uvicorn dashboard.app:app --host 127.0.0.1 --port 8000 >> logs\dashboard_stdout.log 2>&1"

Start-Sleep -Seconds 5

$check = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($check) {
    Write-Output "Dashboard started successfully on port 8000."
} else {
    Write-Output "WARNING: Dashboard did not bind to port 8000. Check logs\dashboard_stdout.log for errors."
}