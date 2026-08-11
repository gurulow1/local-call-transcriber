@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_windows.ps1"
set "launcher_exit=%ERRORLEVEL%"

if not "%launcher_exit%"=="0" (
  echo.
  echo Setup or launch failed. See logs\setup.log for details.
  pause
)

exit /b %launcher_exit%
