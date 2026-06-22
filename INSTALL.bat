@echo off
REM ============================================================================
REM Tired Market - First-Time Install / Setup
REM
REM Run this ONCE after extracting the folder. It will:
REM   1. Verify Python is installed
REM   2. Install required Python packages (one-time, takes a few minutes)
REM   3. Migrate your old data from %APPDATA%\TiredMarket if present
REM   4. Clean up the old AppData folder so nothing gets left behind
REM   5. Create the local data\ folder
REM   6. Launch the app
REM
REM After this finishes successfully, use START.bat for everyday launches.
REM ============================================================================

setlocal enabledelayedexpansion
title Tired Market - First-Time Install

REM Make sure we run from the script directory (works if double-clicked)
cd /d "%~dp0"

echo.
echo ================================================================
echo   TIRED MARKET - FIRST-TIME INSTALL
echo ================================================================
echo.
echo This will set up Tired Market in this folder:
echo   %CD%
echo.
echo Steps:
echo   [1/5] Check Python is installed
echo   [2/5] Install required packages (Python libraries)
echo   [3/5] Migrate your old data from AppData (if any)
echo   [4/5] Clean up old AppData folder
echo   [5/5] Create local data folder, desktop shortcut, and launch
echo.
pause

REM ============================================================================
REM STEP 1: Verify Python
REM ============================================================================
echo.
echo [1/5] Checking Python installation...
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo.
    echo Please install Python 3.10 or newer from:
    echo   https://www.python.org/downloads/
    echo.
    echo During install, make sure to check "Add Python to PATH".
    echo.
    echo After installing Python, double-click this INSTALL.bat again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo OK: Python %PYVER% found.
echo.

REM ============================================================================
REM STEP 2: Install Python packages
REM ============================================================================
echo [2/5] Installing required packages...
echo This takes 3-5 minutes the first time. Coffee break.
echo.

python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo WARNING: Could not upgrade pip. Continuing anyway...
)

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Package installation failed.
    echo Check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo OK: All packages installed.
echo.

REM ============================================================================
REM STEP 3: Migrate data from old AppData location
REM ============================================================================
echo [3/5] Looking for old data in AppData...
echo.

set OLD_DATA=%APPDATA%\TiredMarket
set NEW_DATA=%CD%\data

if not exist "%OLD_DATA%" (
    echo No old AppData folder found - clean install.
    goto :skip_migrate
)

echo Found old data folder: %OLD_DATA%
echo.

REM Make sure the new data folder exists
if not exist "%NEW_DATA%" mkdir "%NEW_DATA%"

REM Copy root-level files from old AppData (config, portfolio, strategy, etc.)
echo Copying configuration files...
for %%F in (config.json portfolio.json strategy.json ai_memory.json license.json .disclaimer_accepted) do (
    if exist "%OLD_DATA%\%%F" (
        if not exist "%NEW_DATA%\%%F" (
            copy "%OLD_DATA%\%%F" "%NEW_DATA%\%%F" >nul 2>&1
            echo   Migrated: %%F
        )
    )
)

REM Copy contents of old data/ subfolder (database, logs) DIRECTLY into new data folder
REM (old layout had data/data/, new layout is flat data/)
if exist "%OLD_DATA%\data" (
    echo Copying database and history...
    xcopy "%OLD_DATA%\data\*" "%NEW_DATA%\" /E /Y /I /Q >nul 2>&1
    if not errorlevel 1 echo   OK: Database and history copied.
)

REM Copy other AppData subfolders if they exist (snapshots, profiles, etc.)
for %%D in (snapshots profiles crash_reports backups) do (
    if exist "%OLD_DATA%\%%D" (
        if not exist "%NEW_DATA%\%%D" mkdir "%NEW_DATA%\%%D"
        xcopy "%OLD_DATA%\%%D\*" "%NEW_DATA%\%%D\" /E /Y /I /Q >nul 2>&1
        echo   Migrated folder: %%D
    )
)

echo.
echo OK: Old data migration complete.
echo.

REM ============================================================================
REM STEP 4: Clean up old AppData
REM ============================================================================
echo [4/5] Removing old AppData folder...
echo.
echo About to delete: %OLD_DATA%
echo (Your data has been copied to %NEW_DATA%)
echo.
choice /C YN /M "Delete the old AppData folder"
if errorlevel 2 (
    echo Skipped. Old AppData folder kept at:
    echo   %OLD_DATA%
    echo You can delete it manually anytime.
) else (
    rmdir /S /Q "%OLD_DATA%" 2>nul
    if exist "%OLD_DATA%" (
        echo WARNING: Could not fully remove old folder. Some files may be in use.
        echo You can delete it manually later: %OLD_DATA%
    ) else (
        echo OK: Old AppData folder removed.
    )
)
echo.
goto :after_migrate

:skip_migrate
echo.

:after_migrate

REM ============================================================================
REM STEP 5: Create local data folder structure
REM ============================================================================
echo [5/5] Setting up local data folder...
echo.

if not exist "%NEW_DATA%" mkdir "%NEW_DATA%"
if not exist "%NEW_DATA%\logs" mkdir "%NEW_DATA%\logs"
if not exist "%NEW_DATA%\backups" mkdir "%NEW_DATA%\backups"
if not exist "%NEW_DATA%\snapshots" mkdir "%NEW_DATA%\snapshots"

echo OK: Data folder ready at %NEW_DATA%
echo.

REM ============================================================================
REM STEP 6: Create desktop shortcut
REM ============================================================================
echo Creating desktop shortcut...
echo.

set SHORTCUT_NAME=Tired Market.lnk
set DESKTOP=%USERPROFILE%\Desktop
set TARGET=%CD%\START.bat
set ICON=%CD%\tired_market.ico

REM Use PowerShell to create the .lnk file (built into Windows, no extras needed)
powershell -NoProfile -Command ^
    "$WshShell = New-Object -ComObject WScript.Shell;" ^
    "$Shortcut = $WshShell.CreateShortcut('%DESKTOP%\%SHORTCUT_NAME%');" ^
    "$Shortcut.TargetPath = '%TARGET%';" ^
    "$Shortcut.WorkingDirectory = '%CD%';" ^
    "$Shortcut.IconLocation = '%ICON%';" ^
    "$Shortcut.Description = 'Tired Market - Personal Stock Analysis';" ^
    "$Shortcut.WindowStyle = 7;" ^
    "$Shortcut.Save()" 2>nul

if exist "%DESKTOP%\%SHORTCUT_NAME%" (
    echo OK: Desktop shortcut created.
) else (
    echo WARNING: Could not create desktop shortcut. Not a problem - you can
    echo still launch with START.bat or create a shortcut manually.
)
echo.

REM ============================================================================
REM Done. Launch the app.
REM ============================================================================
echo ================================================================
echo   INSTALL COMPLETE
echo ================================================================
echo.
echo From now on, double-click START.bat to launch the app.
echo.
echo Launching Tired Market...
echo.
timeout /t 3 >nul

start "" pythonw tired_market.py

REM If pythonw isn't available, fall back to regular python
if errorlevel 1 (
    start "" python tired_market.py
)

echo App launched. You can close this window.
timeout /t 5 >nul
exit /b 0
