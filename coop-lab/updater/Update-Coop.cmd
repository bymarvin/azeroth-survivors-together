@echo off
rem Azeroth Survivors Together co-op updater: run it inside your game folder (next to Start-Coop.cmd).
rem Pauses at the end only when started by double-click; pass --no-pause to skip it.
setlocal
cd /d "%~dp0"
set "COOP_PYTHON=%~dp0.coop-runtime\python.exe"
if not exist "%COOP_PYTHON%" set "COOP_PYTHON=%~dp0..\.coop-runtime\python.exe"
if exist "%COOP_PYTHON%" goto run
echo [FAILED] This is not an Azeroth Survivors Together co-op folder.
echo [ INFO ] Extract the update into your game folder (where Start-Coop.cmd is) and run Update-Coop.cmd there.
set "CODE=2"
goto finish
:run
"%COOP_PYTHON%" -I -B "%~dp0coop-update\update.py"
set "CODE=%ERRORLEVEL%"
:finish
if /i "%~1"=="--no-pause" exit /b %CODE%
setlocal EnableDelayedExpansion
set "LINE=!CMDCMDLINE!"
rem A double-click starts cmd with /c; a terminal or script keeps its own window.
if "!LINE:/c =!"=="!LINE!" exit /b %CODE%
echo.
pause
exit /b %CODE%
