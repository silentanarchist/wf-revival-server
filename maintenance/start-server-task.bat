@echo off
title Warpforge Revival - start the server
netstat -ano | findstr /r /c:":8780 .*LISTENING" >nul
if not errorlevel 1 (
    echo.
    echo  A Warpforge Revival server is already running ^(port 8780 is in use^). Nothing was started.
    echo  If it is a start_server.bat window, close that window first.
    echo.
    pause
    exit /b
)
schtasks /run /tn "Warpforge Revival server"
if errorlevel 1 (
    echo.
    echo  Could not start it. Is the automatic start set up ^(setup-autostart.bat^)?
) else (
    echo.
    echo  Started. The website should answer within a minute; the server writes to logs\server.log.
)
echo.
pause
