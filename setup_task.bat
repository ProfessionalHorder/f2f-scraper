@echo off
rem ============================================================
rem  One-time installer: registers the Windows Task Scheduler
rem  task "f2f-scraper-daily" (runs daily at 03:00).
rem
rem  If you get "Access Denied", right-click this file and
rem  choose "Run as administrator".
rem ============================================================

setlocal
set "TASKNAME=f2f-scraper-daily"
set "SCRIPTDIR=%~dp0"
set "SCRIPTDIR=%SCRIPTDIR:~0,-1%"

if not exist "%SCRIPTDIR%\run_scheduled.bat" (
    echo [!] run_scheduled.bat not found next to this file.
    echo     Put both files in the same folder and retry.
    pause
    exit /b 1
)

schtasks /Query /TN "%TASKNAME%" >nul 2>nul
if not errorlevel 1 (
    echo Task "%TASKNAME%" already exists - replacing it.
    schtasks /Delete /TN "%TASKNAME%" /F >nul
)

schtasks /Create /TN "%TASKNAME%" /TR "\"%SCRIPTDIR%\run_scheduled.bat\"" /SC DAILY /ST 03:00
if errorlevel 1 (
    echo.
    echo [!] Failed to create the task. If you saw "Access Denied",
    echo     right-click setup_task.bat and choose "Run as administrator".
    pause
    exit /b 1
)

echo.
echo Task "%TASKNAME%" created - runs daily at 11:00.
echo.
echo Useful commands:
echo   schtasks /Run   /TN "%TASKNAME%"                        run it right now
echo   schtasks /Query /TN "%TASKNAME%" /V /FO LIST            show details
echo   schtasks /Delete /TN "%TASKNAME%" /F                    remove it
echo.
echo Tip: in Task Scheduler, open the task, tab "Settings", and tick
echo   "Run task as soon as possible after a scheduled start is missed"
echo so the run happens when the PC wakes up if it was off at 03:00.
echo.
pause
endlocal
