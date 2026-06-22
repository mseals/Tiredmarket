@echo off
REM ============================================================================
REM Tired Market - Everyday Launcher
REM
REM Double-click this to start the app. If something breaks, the window stays
REM open so you can see the error message.
REM
REM First time? Run INSTALL.bat instead.
REM ============================================================================

setlocal
title Tired Market
cd /d "%~dp0"

REM Check Python is still available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not available.
    echo.
    echo Run INSTALL.bat to set up, or install Python from python.org.
    echo.
    pause
    exit /b 1
)

REM Check the main script exists
if not exist "tired_market.py" (
    echo ERROR: tired_market.py not found.
    echo This launcher must be in the same folder as the app.
    echo.
    pause
    exit /b 1
)

REM Launch with pythonw (no console window). If it crashes, show the error.
REM We use python (not pythonw) so any startup errors are visible.
echo Starting Tired Market...
echo (This window will close when the app is ready.)
echo.

REM Try pythonw first - silent launch
start "" pythonw tired_market.py
if errorlevel 1 (
    echo pythonw failed, trying python...
    python tired_market.py
    if errorlevel 1 (
        echo.
        echo ============================================================
        echo   APP CRASHED OR FAILED TO LAUNCH
        echo ============================================================
        echo.
        echo See the error message above.
        echo.
        echo If you don't see one, try running this command directly
        echo to see what's wrong:
        echo.
        echo   python tired_market.py
        echo.
        pause
        exit /b 1
    )
)

REM Brief pause so user sees the "starting" message, then close
timeout /t 2 >nul
exit /b 0
