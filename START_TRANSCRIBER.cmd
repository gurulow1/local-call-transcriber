@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo Windows PowerShell was not found. This launcher requires Windows 10 or newer.
  pause
  exit /b 1
)

echo Local Call Transcriber - verified Windows/NVIDIA pilot
echo First start can download about 3.9 GB and can take a while.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_and_run_windows.ps1"
set "launcher_exit=%ERRORLEVEL%"

if not "%launcher_exit%"=="0" (
  echo.
  echo Setup or runtime stopped with exit code %launcher_exit%.
  echo Read the message above before retrying.
)

echo.
pause
exit /b %launcher_exit%
