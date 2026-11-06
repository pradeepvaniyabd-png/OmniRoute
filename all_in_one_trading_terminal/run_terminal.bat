@echo off
setlocal EnableExtensions
title All-in-One Trading Super-Terminal
cd /d "%~dp0"

rem ===========================================================================
rem  One-click launcher
rem    run_terminal.bat                   -> web terminal  http://127.0.0.1:8800
rem    run_terminal.bat --start           -> web terminal + enable trading now
rem    run_terminal.bat classic           -> classic Streamlit dashboard (:8501)
rem    run_terminal.bat headless          -> trading engine only (VPS mode)
rem    run_terminal.bat headless --start  -> headless + enable trading now
rem  First run: creates .venv, installs requirements, creates .env from template.
rem  Later runs: reinstalls only when requirements.txt changes.
rem ===========================================================================

set "CLASSIC_PORT=8501"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.11+ ^(64-bit^) was not found on PATH.
    echo         Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    %PY% -c "import sys,struct;sys.exit(0 if sys.version_info[:2]>=(3,11) and struct.calcsize('P')==8 else 1)"
    if errorlevel 1 (
        echo [ERROR] Python 3.11 or newer, 64-bit, is required ^(MetaTrader5 package^).
        pause
        exit /b 1
    )
    echo [SETUP] Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "VPY=.venv\Scripts\python.exe"

set "REQ_HASH="
for /f "usebackq delims=" %%H in (`.venv\Scripts\python.exe -c "import hashlib;print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())"`) do set "REQ_HASH=%%H"
set "OLD_HASH="
if exist ".venv\requirements.sha256" set /p OLD_HASH=<".venv\requirements.sha256"
if not "%REQ_HASH%"=="%OLD_HASH%" (
    echo [SETUP] Installing / updating dependencies - this can take a few minutes ...
    "%VPY%" -m pip install --upgrade pip
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your internet connection and retry.
        pause
        exit /b 1
    )
    > ".venv\requirements.sha256" echo %REQ_HASH%
)

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo [SETUP] Created .env from .env.example - edit it with your MT5 and Telegram details.
)

if /i "%~1"=="headless" (
    echo [START] Headless trading worker - press Ctrl+C to stop ...
    "%VPY%" background_worker.py %2 %3
    goto :finish
)

if /i "%~1"=="classic" (
    echo [START] Classic dashboard on http://localhost:%CLASSIC_PORT%  - press Ctrl+C to stop.
    "%VPY%" -m streamlit run app.py --server.port %CLASSIC_PORT% --server.headless false --browser.gatherUsageStats false
    goto :finish
)

echo [START] Web terminal - the browser opens automatically. Press Ctrl+C in this window to stop.
"%VPY%" server.py %*

:finish
if errorlevel 1 (
    echo.
    echo [ERROR] The terminal exited with an error. Details: logs\terminal.log
    pause
)
endlocal
