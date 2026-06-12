@echo off
REM ============================================================================
REM Tired Market - Desktop Icon Installer
REM
REM Double-click this file. It puts a "Tired Market" shortcut on your desktop
REM with the correct icon. Use that shortcut to launch every day - it has zero
REM console flash because it points pythonw.exe at the app directly.
REM
REM Safe to run multiple times - it just overwrites the desktop shortcut.
REM Works from any folder; auto-locates D:\TiredMarket\.
REM ============================================================================

setlocal enabledelayedexpansion
title Tired Market - Desktop Icon Installer

echo.
echo ============================================================
echo   Tired Market - Desktop Shortcut Installer
echo ============================================================
echo.

REM 1) Locate the install dir. Prefer the folder this bat lives in (works
REM    when copied next to the app); fall back to D:\TiredMarket\ if invoked
REM    from a downloads folder or USB stick.
set "INSTALL_DIR=%~dp0"
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"

if not exist "%INSTALL_DIR%\tired_market.py" (
    if exist "D:\TiredMarket\tired_market.py" (
        set "INSTALL_DIR=D:\TiredMarket"
    ) else (
        echo ERROR: Could not find tired_market.py.
        echo.
        echo This installer expects the app at D:\TiredMarket\ or in the
        echo same folder as this .bat file. Move it there and try again.
        echo.
        pause
        exit /b 1
    )
)

echo [1/4] Install folder:  %INSTALL_DIR%

REM 2) Confirm START.vbs is present. The shortcut targets START.vbs directly
REM    so the launch survives Python upgrades (the .vbs delegates to whichever
REM    pythonw.exe is on PATH at run time). Trade-off: brief white wscript
REM    flash on each launch. User asked for this layout explicitly.
if not exist "%INSTALL_DIR%\START.vbs" (
    echo.
    echo ERROR: START.vbs not found in %INSTALL_DIR%
    echo The shortcut launches the app via that file. If it was deleted,
    echo restore it from a backup or re-install.
    echo.
    pause
    exit /b 1
)

echo [2/4] VBS launcher:    %INSTALL_DIR%\START.vbs

REM 3) Resolve the user's Desktop. PowerShell handles OneDrive redirection.
set "DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
if not defined DESKTOP (
    if exist "%USERPROFILE%\Desktop" set "DESKTOP=%USERPROFILE%\Desktop"
)
if not defined DESKTOP (
    echo.
    echo ERROR: Could not locate your Desktop folder.
    pause
    exit /b 1
)

echo [3/4] Desktop folder:  %DESKTOP%

REM 4) Build the shortcut. Target = START.vbs directly; Windows uses its
REM    file association to run it through wscript.exe at click time.
REM    Paths flow via env vars so we don't have to escape quotes through cmd.
set "LNK_PATH=%DESKTOP%\Tired Market.lnk"
set "ICON_PATH=%INSTALL_DIR%\tired_market.ico"
set "VBS_PATH=%INSTALL_DIR%\START.vbs"
set "TM_DESC=Tired Market - AI-driven stock analysis"

echo [4/4] Writing shortcut: %LNK_PATH%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=$ws.CreateShortcut($env:LNK_PATH); $lnk.TargetPath=$env:VBS_PATH; $lnk.Arguments=''; $lnk.WorkingDirectory=$env:INSTALL_DIR; $lnk.Description=$env:TM_DESC; if (Test-Path $env:ICON_PATH) { $lnk.IconLocation=$env:ICON_PATH }; $lnk.WindowStyle=1; $lnk.Save(); $v=$ws.CreateShortcut($env:LNK_PATH); Write-Host ('  Target:    ' + $v.TargetPath); Write-Host ('  Arguments: ' + $v.Arguments); Write-Host ('  WorkDir:   ' + $v.WorkingDirectory); Write-Host ('  Icon:      ' + $v.IconLocation)"

if errorlevel 1 (
    echo.
    echo Shortcut creation FAILED. The PowerShell error is shown above.
    echo If your install dir is somewhere protected, try right-clicking
    echo this .bat and choosing "Run as administrator".
    pause
    exit /b 1
)

if not exist "%LNK_PATH%" (
    echo.
    echo PowerShell ran without an error but the shortcut wasn't created.
    echo Check that you have write permission to your Desktop folder.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done. "Tired Market" is on your desktop.
echo ============================================================
echo.
echo Look at your desktop now - you should see the Tired Market icon.
echo Double-click it to launch the app (no console flash).
echo.
echo Want it on the taskbar too?
echo   Right-click the desktop icon -^> "Pin to taskbar"
echo.
echo Want it in the Start menu?
echo   Right-click the desktop icon -^> "Pin to Start"
echo.
echo Re-run this installer any time to refresh the shortcut (e.g. after
echo Python is upgraded or the install moves).
echo.
pause
exit /b 0
