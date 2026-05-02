@echo off
setlocal
pushd "%~dp0"

echo.
echo ============================================================
echo   Soulmask Manager - launcher
echo ============================================================
echo.

REM ---- Python check ---------------------------------------------------------
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
if "%PYVER%"=="" (
    echo [error] 'python' not found on PATH.
    echo         Install Python 3.11+ and ensure it's on PATH.
    echo         Test from a fresh cmd:  python --version
    echo.
    pause
    popd
    endlocal
    exit /b 1
)
echo [env]   %PYVER%
echo [env]   Working dir: %CD%

REM ---- Virtual environment --------------------------------------------------
if not exist "venv\Scripts\activate.bat" (
    echo [venv]  Not found. Creating in venv\ ...
    python -m venv venv
    if errorlevel 1 (
        echo [error] Failed to create venv.
        pause
        popd
        endlocal
        exit /b 1
    )
    echo [venv]  Created.
) else (
    echo [venv]  Found existing venv\
)

call venv\Scripts\activate.bat

REM ---- Dependencies ---------------------------------------------------------
echo [deps]  Installing/updating from requirements.txt ...
pip install --disable-pip-version-check --requirement requirements.txt
if errorlevel 1 (
    echo [error] pip install failed. See output above.
    pause
    popd
    endlocal
    exit /b 1
)
echo [deps]  All dependencies satisfied.

REM ---- Launch ---------------------------------------------------------------
echo.
echo [start] Launching Soulmask Manager...
echo [start] URL (this machine):  http://localhost:5000
echo [start] URL (from LAN):      http://%COMPUTERNAME%:5000
echo [start] Logs streaming below + written to logs\manager.log
echo [start] Press Ctrl+C in this window to stop.
echo.

:server-loop
REM Pre-boot: detect crash loops and roll back to last_known_good if
REM the previous launch failed to reach stable boot. See bootloader.py.
REM `-u` forces unbuffered stdout/stderr so terminal output streams in
REM real time. Without it the manager appears to hang on update / pip
REM install while the parent buffer fills.
python -u bootloader.py
python -u -m manager
set "EXITCODE=%errorlevel%"

REM Exit code 99 = the manager UI requested a restart. Loop and relaunch.
if "%EXITCODE%"=="99" (
    echo.
    echo [restart] Manager requested a restart. Relaunching in 2 seconds...
    timeout /t 2 /nobreak > nul 2>&1
    echo.
    goto server-loop
)

echo.
if "%EXITCODE%"=="0" (
    echo [stop]  Manager exited cleanly.
) else (
    echo [error] Manager exited with code %EXITCODE%
    echo         If a Python traceback is shown above, that's the cause.
    echo         Also check logs\manager.log if anything got that far.
    pause
)
popd
endlocal
