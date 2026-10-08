@echo off
setlocal
cd /d "%~dp0"
title Warpforge Revival - set up the nightly data backup

echo.
echo  Warpforge Revival - nightly backup of the server's data
echo  --------------------------------------------------------
echo  Run this once, on the PC that runs the server.
echo  Every night at 4:12 it saves the server's data folder (accounts, players, decks,
echo  bans, chat, custom cards) as one dated zip in
echo     C:\WarpforgeRevival-backups
echo  The newest 10 backups are kept. The folder's backup-log.txt says how each night went.
echo.

set "PY="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
    echo  Python was not found on this PC. The server needs it too - is this the server PC?
    goto :end
)
set "SCRIPT=%~dp0backup-data.py"

echo  Making a first backup now ...
"%PY%" "%SCRIPT%"
if errorlevel 1 (
    echo.
    echo  The first backup did not work - the message above says why. Nothing was scheduled.
    goto :end
)

echo.
echo  Scheduling it for every night at 4:12 ...
schtasks /create /f /tn "Warpforge Revival data backup" /sc daily /st 04:12 /tr "\"%PY%\" \"%SCRIPT%\""
if errorlevel 1 (
    echo  Scheduling failed - the message above says why. Backups can still be made by hand:
    echo     "%PY%" "%SCRIPT%"
    goto :end
)
echo.
echo  Done. It runs while you are signed in to this PC ^(like the server itself^).
echo  To see or remove it: Task Scheduler, "Warpforge Revival data backup".
echo  To restore: stop the server, unzip a backup's "data" folder over the server's
echo  data folder, start the server.

:end
echo.
pause
