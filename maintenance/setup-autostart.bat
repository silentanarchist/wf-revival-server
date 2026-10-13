@echo off
setlocal
cd /d "%~dp0"
title Warpforge Revival - automatic start and Windows Update hours

rem Needs administrator rights (Task Scheduler and Windows Update settings): ask for them if missing.
net session >nul 2>&1
if errorlevel 1 (
    echo  Asking for administrator rights ...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
    )
    exit /b
)

if /i "%~1"=="remove" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-autostart.ps1" -Remove
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-autostart.ps1"
)
if errorlevel 1 echo. & echo  It did not finish - the red message above says why.
echo.
pause
