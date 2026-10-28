@echo off
setlocal
cd /d "%~dp0..\.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Python virtual environment not found.
  exit /b 1
)
rem Binds to 127.0.0.1 unless IBO_BIND_HOST (in .env or the environment) says
rem otherwise -- see dashboard\serve.py and packaging\OPERATIONS.md.
"%PY%" -m dashboard.serve --port 8000
