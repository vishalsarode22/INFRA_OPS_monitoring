@echo off
rem Creates a Task Scheduler task for THIS Windows user (no admin rights needed)
rem that runs vpn_reconnect.ps1 every 2 minutes, without a window.
rem
rem   install_vpn_reconnect_task.bat                         AG VPN on the IBOPS VM (PS4 probe)
rem   install_vpn_reconnect_task.bat CEAT 10.x.x.x:3300 CEAT_VPN_PASSWORD   another VPN / PC
rem
rem Remove:  schtasks /delete /tn "IBOPS VPN reconnect NAME" /f
setlocal
set "HERE=%~dp0"
set "NAME=%~1"
if "%NAME%"=="" set "NAME=AG"
set "ARGS=-Name %NAME%"
if not "%~2"=="" set "ARGS=%ARGS% -Probe %~2"
if not "%~3"=="" set "ARGS=%ARGS% -PasswordVar %~3"
set "VBS=%HERE%run_hidden_%NAME%.vbs"
> "%VBS%" echo CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -File ""%HERE%vpn_reconnect.ps1"" %ARGS%", 0, False
schtasks /create /tn "IBOPS VPN reconnect %NAME%" /sc minute /mo 2 /tr "wscript.exe \"%VBS%\"" /f
if errorlevel 1 (
  echo Could not create the task. Copy the output above to Claude.
  exit /b 1
)
echo.
echo Task "IBOPS VPN reconnect %NAME%" created: checks the VPN every 2 minutes.
echo Log: runtime\vpn_reconnect_%NAME%.log
exit /b 0
