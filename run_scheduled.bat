@echo off
rem ============================================================
rem  f2f-scraper - scheduled run wrapper (portable, relative path)
rem  Works wherever this .bat lives. Output log: logs\scheduler.log
rem ============================================================
setlocal enabledelayedexpansion

rem --- Resolve the folder THIS batch file lives in (no hardcoded path) ---
set "SCRIPTDIR=%~dp0"
set "SCRIPTDIR=%SCRIPTDIR:~0,-1%"

set "LOGDIR=%SCRIPTDIR%\logs"
set "LOCKDIR=%SCRIPTDIR%\run.lock"

rem --- Force UTF-8 so emoji names don't crash the scripts ---
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

rem --- lock check: skip if active, remove if stale (>2h) ---
if exist "%LOCKDIR%" (
    for /f "usebackq" %%e in (`powershell -NoProfile -Command ^
        "if ((Get-Item '%LOCKDIR%').CreationTime.AddHours(2) -lt (Get-Date)) { 'expired' } else { 'active' }"`) do set "LOCKSTATE=%%e"
    if "!LOCKSTATE!"=="expired" (
        echo [%date% %time%] stale lock detected ^(older than 2h^) - removing >> "%LOGDIR%\scheduler.log"
        rmdir /s /q "%LOCKDIR%"
    ) else (
        echo [%date% %time%] previous run still active - skipping >> "%LOGDIR%\scheduler.log"
        goto :eof
    )
)

mkdir "%LOCKDIR%" 2>nul
if errorlevel 1 (
    echo [%date% %time%] could not acquire lock - skipping >> "%LOGDIR%\scheduler.log"
    goto :eof
)

echo. >> "%LOGDIR%\scheduler.log"
echo ================ %date% %time% ================ >> "%LOGDIR%\scheduler.log"

cd /d "%SCRIPTDIR%"
if errorlevel 1 (
    echo [%date% %time%] ERROR: could not cd to script folder >> "%LOGDIR%\scheduler.log"
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