@echo off
cd /d D:\SAP_BASIS_MONITOR
call venv\Scripts\activate.bat
python main.py >> logs\scheduled_run.log 2>&1