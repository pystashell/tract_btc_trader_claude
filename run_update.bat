@echo off
REM ============================================================
REM Paul Wei Hyperliquid tracker - weekly freshness check
REM (called by Windows Task Scheduler).
REM Only fetches if last successful update was >= 21 days ago;
REM otherwise it just checks and does nothing.
REM Manual updates: run update.py directly - not affected by this.
REM ============================================================
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"
set "LOG=%~dp0data\logs\scheduled_update.log"
echo.>> "%LOG%"
echo ============================================================>> "%LOG%"
echo [%date% %time%] scheduled check start>> "%LOG%"
py -3.12 -X utf8 update.py --max-age-days 21 >> "%LOG%" 2>&1
echo [%date% %time%] scheduled check done, exit=%ERRORLEVEL%>> "%LOG%"
