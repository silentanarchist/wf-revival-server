@echo off
title Warpforge Revival - start the server
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
