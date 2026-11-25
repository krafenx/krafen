@echo off
setlocal EnableExtensions
title Krafen Stream Recorder

cd /d "%~dp0.."

if not exist ".env" (
    echo [ERROR] .env file not found in %CD%
    pause
    exit /b 1
)

set "PY_BIN="
if exist ".venv\Scripts\python.exe" set "PY_BIN=.venv\Scripts\python.exe"
if not defined PY_BIN if exist "venv\Scripts\python.exe" set "PY_BIN=venv\Scripts\python.exe"

if not defined PY_BIN (
    where py >nul 2>&1
    if not errorlevel 1 set "PY_BIN=py -3"
)
if not defined PY_BIN (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_BIN=python"
)
if not defined PY_BIN (
    echo [ERROR] Python 3.11+ not found. Install Python and dependencies from recorder\requirements.txt.
    pause
    exit /b 1
)

echo [Krafen Recorder] Starting from %CD% (%PY_BIN%)

set /a CRASH_STREAK=0

:loop
%PY_BIN% "recorder\recorder.py" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto end
if "%RC%"=="2" (
    echo [Krafen Recorder] Fatal configuration error ^(code 2^). Fix .env and restart.
    pause
    exit /b 2
)

set /a CRASH_STREAK+=1
if %CRASH_STREAK% GEQ 10 (
    echo [Krafen Recorder] Too many consecutive failures ^(%CRASH_STREAK%^). Stopping restart loop.
    pause
    exit /b %RC%
)

echo [Krafen Recorder] Exited with code %RC% ^(attempt %CRASH_STREAK%/10^). Restarting in 15s... ^(Ctrl+C to stop^)
ping -n 16 127.0.0.1 >nul
goto loop

:end
echo [Krafen Recorder] Stopped.
