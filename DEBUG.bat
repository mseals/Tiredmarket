@echo off
REM ============================================================================
REM Tired Market - Debug Launcher
REM
REM Same as START.bat but runs with the console window OPEN so you can see
REM any error messages, print statements, or crash output.
REM
REM Use this if the app won't start, crashes immediately, or behaves weird
REM and you want to see what's happening.
REM ============================================================================

title Tired Market - Debug Mode
cd /d "%~dp0"

echo ================================================================
echo   TIRED MARKET - DEBUG MODE
echo ================================================================
echo.
echo Running with console output visible.
echo Errors and messages will appear here.
echo.
echo Close the app window normally to stop, or press Ctrl+C here.
echo ================================================================
echo.

python tired_market.py

echo.
echo ================================================================
echo   APP HAS EXITED
echo ================================================================
echo.
echo If you saw an error above, copy it and tell Claude.
echo.
pause
