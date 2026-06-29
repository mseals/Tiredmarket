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

REM v4.14.6.111-process-identity: launch via the renamed interpreter
REM (venv\Scripts\TiredMarket.exe) so the app shows as "TiredMarket.exe" in
REM Task Manager, distinct from other Python apps. This is a ONE-TIME launcher
REM copy of pythonw.exe (interpreter only, no app code) — NOT a build.
set "TMEXE=%~dp0venv\Scripts\TiredMarket.exe"

REM Check the renamed interpreter exists (falls back to system python if not)
if not exist "%TMEXE%" (
    echo NOTE: venv\Scripts\TiredMarket.exe not found - falling back to system python.
    python --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python is not available.
        echo Run INSTALL.bat to set up, or install Python from python.org.
        echo.
        pause
        exit /b 1
    )
    set "TMEXE=pythonw"
)

REM Check the main script exists
if not exist "tired_market.py" (
    echo ERROR: tired_market.py not found.
    echo This launcher must be in the same folder as the app.
    echo.
    pause
    exit /b 1
)

echo Starting Tired Market...
echo (This window will close when the app is ready.)
echo.

REM Silent launch via the renamed interpreter (windowless).
start "" "%TMEXE%" tired_market.py
if errorlevel 1 (
    echo launch failed, trying system python...
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
