# Warpforge Revival - start the server by itself, and keep Windows Update from restarting the PC
# while people play. Run through setup-autostart.bat (it asks for administrator rights).
#
#   setup-autostart.bat          set up (or update) both
#   setup-autostart.bat remove   undo the automatic start (Windows Update settings are left as they are)
#
# What it sets up:
#  1. A Task Scheduler task "Warpforge Revival server" that starts the server one minute after
#     Windows starts, without anyone signing in, as your Windows account (it asks for its password
#     once; Windows keeps it, this script does not). It has no time limit and Windows tries again
#     if it fails to start. There is no server window in this mode: everything the server prints
#     goes to logs\server.log, and anything printed before that (a crash at start-up) to
#     logs\console.log. Use start-server-task.bat / stop-server-task.bat to start or stop it.
#  2. Windows Update "active hours" (no automatic restarts in that window; Windows allows at most
#     18 hours) and "no automatic restart while someone is signed in".
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName    = "Warpforge Revival server"
$Port        = 8780
$ActiveStart = 8      # 8:00 in the morning ...
$ActiveEnd   = 2      # ... until 2:00 at night (18 hours, the most Windows allows)

$ServerDir = Split-Path -Parent $PSScriptRoot
Write-Host ""
Write-Host " Warpforge Revival - automatic start and Windows Update hours"
Write-Host " -------------------------------------------------------------"
Write-Host " Server folder: $ServerDir"
Write-Host ""

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host " The automatic start is removed (and the server it had started is stopped)."
        Write-Host " Start the server by hand again with start_server.bat."
    } else {
        Write-Host " There was no automatic start to remove."
    }
    return
}

if (-not (Test-Path (Join-Path $ServerDir "warpforge_server.py"))) {
    throw "warpforge_server.py is not in $ServerDir - keep this script in the server's maintenance folder."
}
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python -or $python -like "*WindowsApps*") {
    throw "Python was not found (or only the Microsoft Store shortcut). The server needs it - is this the server PC?"
}
Write-Host " Python: $python"

# --- 1. the automatic start --------------------------------------------------------------------
$logs = Join-Path $ServerDir "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$cmdLine = "/c cd /d `"$ServerDir`" && `"$python`" warpforge_server.py --port $Port >> logs\console.log 2>&1"
$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdLine -WorkingDirectory $ServerDir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT1M"                                   # give the network a minute after start-up
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$user = "$env:USERDOMAIN\$env:USERNAME"
Write-Host ""
Write-Host " The server will run as your Windows account ($user), also when nobody is signed in."
Write-Host " Windows asks for that account's password once (a PIN does not work here)."
$cred = Get-Credential -UserName $user -Message "Password of $user, so the server can start before anyone signs in"
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Highest -Force | Out-Null
Write-Host " Automatic start set up: task `"$TaskName`" (Task Scheduler)."

# --- 2. Windows Update -------------------------------------------------------------------------
$ux = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
New-Item -Path $ux -Force | Out-Null
Set-ItemProperty -Path $ux -Name ActiveHoursStart -Type DWord -Value $ActiveStart
Set-ItemProperty -Path $ux -Name ActiveHoursEnd -Type DWord -Value $ActiveEnd
Set-ItemProperty -Path $ux -Name SmartActiveHoursState -Type DWord -Value 0     # keep these hours, do not guess them
$au = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
New-Item -Path $au -Force | Out-Null
Set-ItemProperty -Path $au -Name NoAutoRebootWithLoggedOnUsers -Type DWord -Value 1
Write-Host " Windows Update: no automatic restarts from ${ActiveStart}:00 to ${ActiveEnd}:00, nor while someone is signed in."
Write-Host " Updates still install; a restart outside those hours brings the server back by itself."

# --- start it now? -------------------------------------------------------------------------------
Write-Host ""
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host " A server is already running (port $Port is in use) - probably the start_server.bat window."
    Write-Host " Close that window, then run start-server-task.bat (or just restart the PC)."
} else {
    $answer = Read-Host " Start the server through the task now? (Y/N)"
    if ($answer -match '^[Yy]') {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host " Started. It writes to logs\server.log; the website should answer within a minute."
    }
}
Write-Host ""
Write-Host " From now on, start or stop the server with start-server-task.bat / stop-server-task.bat"
Write-Host " (not start_server.bat while the task is running: both would want port $Port)."
Write-Host " To undo the automatic start: setup-autostart.bat remove"
