@echo off
title Warpforge Revival server
cd /d "%~dp0"
rem Never a second server on the same port (Windows would let both run and split the players).
netstat -ano | findstr /r /c:":8780 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  A Warpforge Revival server is already running on this PC ^(port 8780 is in use^),
    echo  probably the automatic start. Nothing was started.
    echo  To restart it: maintenance\stop-server-task.bat, then maintenance\start-server-task.bat.
    echo.
    pause
    exit /b
)
python warpforge_server.py --port 8780
pause
