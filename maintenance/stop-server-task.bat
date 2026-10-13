@echo off
title Warpforge Revival - stop the server
echo  This stops the server for everyone (matches in progress end).
choice /m " Stop it now"
if errorlevel 2 goto :end
schtasks /end /tn "Warpforge Revival server"
rem schtasks /end stops cmd.exe; the server (python) it started is stopped too:
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8780 .*LISTENING"') do taskkill /pid %%P /f >nul 2>&1
echo.
echo  Stopped. Start it again with start-server-task.bat (or it starts by itself after a restart).
:end
echo.
pause
