@echo off
REM Run this from the PROJECT ROOT: D:\SAP_BASIS_MONITOR> packaging\build.bat
REM Requires: venv activated, pyinstaller installed (pip install pyinstaller --break-system-packages
REM is not needed on Windows -- just `pip install pyinstaller`)

setlocal

echo === Cleaning previous build ===
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo === Building SAP_BASIS_MONITOR.exe with PyInstaller ===
pyinstaller packaging\SAP_BASIS_MONITOR.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo === Staging loose data files next to the exe ===
REM These are read at runtime relative to the exe's own folder (see
REM utils/paths.py) -- they are NOT embedded inside the exe itself.
xcopy /e /i /y config\templates dist\config\templates
xcopy /e /i /y dashboard\static dist\dashboard\static
copy /y config\thresholds.yaml dist\config\thresholds.yaml
copy /y config\monitoring_tasks.yaml dist\config\monitoring_tasks.yaml
copy /y config\ocr_patterns.yaml dist\config\ocr_patterns.yaml

echo.
echo === Build complete ===
echo dist\SAP_BASIS_MONITOR.exe  (plus its config\ and dashboard\static\ folders)
echo.
echo Next: run packaging\installer.iss with Inno Setup to produce a
echo distributable installer, or just zip the dist\ folder for a
echo portable copy.

endlocal
