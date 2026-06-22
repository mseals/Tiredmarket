@echo off
REM ============================================================================
REM Tired Market — Shortcut Creator (v4.13.62)
REM
REM Run this ONCE. It creates "Tired Market.lnk" in this folder. Double-click
REM that .lnk every day instead of START.vbs / START.bat — it launches with
REM zero console flash because the shortcut points directly at pythonw.exe.
REM
REM After the .lnk exists, you can drag/copy it to your Desktop or pin it to
REM the Start menu / taskbar from there. The shortcut works as long as the
REM install at this path stays in place.
REM ============================================================================

setlocal enabledelayedexpansion
title Tired Market - Shortcut Creator

REM Auto-locate install dir. Prefer the folder this bat lives in (works from
REM any path); fall back to %USERPROFILE%\TiredMarket\ if invoked from a copy
REM elsewhere.
set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

if not exist "%INSTALL_DIR%\tired_market.py" (
    if exist "%USERPROFILE%\TiredMarket\tired_market.py" (
        set "INSTALL_DIR=%USERPROFILE%\TiredMarket"
    ) else (
        echo ERROR: Could not find tired_market.py
        echo.
        echo Put this CREATE_SHORTCUT.bat in the same folder as tired_market.py
        echo and run it again.
        echo.
        pause
        exit /b 1
    )
)

REM v4.13.62.2: Shortcut now targets START.vbs directly so it survives
REM Python upgrades (the .vbs delegates to whichever pythonw.exe is on
REM PATH at run time). Trade-off: brief white wscript flash per launch.
if not exist "%INSTALL_DIR%\START.vbs" (
    echo ERROR: START.vbs not found in %INSTALL_DIR%
    echo The shortcut launches the app via that file. If it was deleted,
    echo restore it from a backup or re-install.
    pause
    exit /b 1
)

set "LNK_PATH=%INSTALL_DIR%\Tired Market.lnk"
set "ICON_PATH=%INSTALL_DIR%\tired_market.ico"
set "VBS_PATH=%INSTALL_DIR%\START.vbs"
set "TM_DESC=Tired Market - AI-driven stock analysis"

echo Install dir : %INSTALL_DIR%
echo Launcher    : %VBS_PATH%
echo Output      : %LNK_PATH%
echo.

REM Build the .lnk via PowerShell's WScript.Shell COM. Paths flow through
REM env vars so we don't need to escape quotes through cmd. The shortcut's
REM Arguments field is empty -- Windows runs the .vbs via its registered
REM file handler (wscript.exe) automatically.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=$ws.CreateShortcut($env:LNK_PATH); $lnk.TargetPath=$env:VBS_PATH; $lnk.Arguments=''; $lnk.WorkingDirectory=$env:INSTALL_DIR; $lnk.Description=$env:TM_DESC; if (Test-Path $env:ICON_PATH) { $lnk.IconLocation=$env:ICON_PATH }; $lnk.WindowStyle=1; $lnk.Save(); $v=$ws.CreateShortcut($env:LNK_PATH); Write-Host ('  Target:    ' + $v.TargetPath); Write-Host ('  Arguments: ' + $v.Arguments); Write-Host ('  WorkDir:   ' + $v.WorkingDirectory); Write-Host ('  Icon:      ' + $v.IconLocation)"

if errorlevel 1 (
    echo.
    echo Shortcut creation failed. Check the error above.
    pause
    exit /b 1
)

if not exist "%LNK_PATH%" (
    echo.
    echo PowerShell ran but the shortcut wasn't created. Try running this
    echo file as Administrator if your install dir is somewhere protected.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done. "Tired Market.lnk" is ready in %INSTALL_DIR%
echo ============================================================
echo.
echo Daily use:
echo   - Double-click "Tired Market.lnk" to launch (zero flash).
echo   - Drag it to your Desktop or pin it to the Start menu / taskbar
echo     if you want quicker access.
echo.
echo You can delete START.vbs and START.bat now if you want — the new
echo .lnk replaces both. Or leave them as fallbacks; nothing depends
echo on them.
echo.
pause
exit /b 0
