@echo off
rem ============================================================
rem  f2f-scraper - scheduled run wrapper
rem  Downloads NEW posts/stories/chats for all creators you
rem  currently follow or are a fan of. Idempotent: existing
rem  files are skipped, so it only fetches new content.
rem
rem  Output log:  logs\scheduler.log
rem ============================================================

setlocal
set "SCRIPTDIR=C:\Users\MikeSpaans\Documents\f2f-scraper-2026"
set "LOGDIR=%SCRIPTDIR%\logs"
set "LOCKDIR=%SCRIPTDIR%\run.lock"

rem --- Force UTF-8 so emoji's in creator names don't crash the scripts ---
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

rem --- lock: skip if a previous run is still active ---
mkdir "%LOCKDIR%" 2>nul
if errorlevel 1 (
    echo [%date% %time%] previous run still active - skipping >> "%LOGDIR%\scheduler.log"
    goto :eof
)

echo. >> "%LOGDIR%\scheduler.log"
echo ================ %date% %time% ================ >> "%LOGDIR%\scheduler.log"

cd /d "%SCRIPTDIR%"
if errorlevel 1 (
    echo [%date% %time%] ERROR: folder not found: %SCRIPTDIR% >> "%LOGDIR%\scheduler.log"
    rmdir "%LOCKDIR%"
    goto :eof
)

where python >nul 2>nul
if errorlevel 1 (
    echo [%date% %time%] ERROR: python not found on PATH >> "%LOGDIR%\scheduler.log"
    rmdir "%LOCKDIR%"
    goto :eof
)

python run_all.py -c cookies.txt -o downloads --scope followed >> "%LOGDIR%\scheduler.log" 2>&1

echo [%date% %time%] finished, exit code %errorlevel% >> "%LOGDIR%\scheduler.log"

rmdir "%LOCKDIR%"
endlocal