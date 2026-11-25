@echo off
setlocal EnableExtensions
title Krafen Recorder Autostart

cd /d "%~dp0.."

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
    echo [ERROR] Python not found in PATH or .venv.
    pause
    exit /b 1
)

if /I "%~1"=="uninstall" (
    %PY_BIN% "recorder\recorder.py" --uninstall-autostart
    goto done
)
if /I "%~1"=="remove" (
    %PY_BIN% "recorder\recorder.py" --uninstall-autostart
    goto done
)
if /I "%~1"=="status" (
    %PY_BIN% "recorder\recorder.py" --status
    goto done
)
if /I "%~1"=="stop" (
    %PY_BIN% "recorder\recorder.py" --stop
    goto done
)
if "%~1"=="" goto do_install
if /I "%~1"=="--no-pause" goto do_install
if /I "%~1"=="install" goto do_install
if /I "%~1"=="--install" goto do_install

echo [ERROR] Unknown argument: %~1
echo Usage: %~nx0 [install^|uninstall^|status^|stop^|--no-pause]
exit /b 2

:do_install
%PY_BIN% "recorder\recorder.py" --install-autostart

:done
if /I not "%~1"=="--no-pause" pause
