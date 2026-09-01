@echo off
setlocal
cd /d "%~dp..\.."
if not exist "venv\Scripts\python.exe" (
  echo Python virtual environment not found.
  exit /b 1
)
venv\Scripts\python.exe -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
