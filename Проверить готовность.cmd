@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PREFLIGHT_PYTHON=.poetry-cache\venv\Scripts\python.exe"
if not exist "%PREFLIGHT_PYTHON%" set "PREFLIGHT_PYTHON=.venv\Scripts\python.exe"
if not exist "%PREFLIGHT_PYTHON%" (
  echo [FAIL] Локальный Python не найден. Сначала запустите "Запустить транскрибатор.cmd".
  pause
  exit /b 1
)

"%PREFLIGHT_PYTHON%" scripts\preflight.py %*
set "PREFLIGHT_EXIT=%ERRORLEVEL%"
pause
exit /b %PREFLIGHT_EXIT%
