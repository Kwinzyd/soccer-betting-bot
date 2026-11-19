# Registers the odds logger with Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File install_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall
#
# Runs every 3 hours. One run costs ~4 API calls and ~55s (the 13s spacing is
# the 5 req/min rate limit), so this is nowhere near any quota. Odds move most
# in the hours before kickoff; 3-hourly catches the shape of that drift without
# hammering the endpoint.

param([switch]$Uninstall)

$TaskName = "balldontlie-odds-logger"
$SettleTaskName = "balldontlie-odds-settle"
$RosterTaskName = "balldontlie-roster-sync"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$Script = Join-Path $RootDir "bot.py"
$SettleScript = Join-Path $ScriptDir "settle.py"
$RosterScript = Join-Path $RootDir "fetch_rosters.py"

if ($Uninstall) {
    schtasks /Delete /TN $TaskName /F
    schtasks /Delete /TN $SettleTaskName /F
    schtasks /Delete /TN $RosterTaskName /F
    Write-Host "Removed '$TaskName', '$SettleTaskName', and '$RosterTaskName'."
    exit 0
}

if (-not (Test-Path $Script)) {
    Write-Error "Cannot find $Script"
    exit 1
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Error "python not found on PATH"; exit 1 }

# -u so the run log is written unbuffered even if a run is killed midway
$action = New-ScheduledTaskAction -Execute $py `
    -Argument "-u `"$Script`"" -WorkingDirectory $ScriptDir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Hours 3)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Logs 1X2 odds from balldontlie for upcoming fixtures" -Force

# Settlement pulls a whole season of results (~16 calls, ~3.5 min), far too
# heavy for the 3-hourly logger. Once a day, overnight, is plenty -- results
# do not change after a match ends.
$settleAction = New-ScheduledTaskAction -Execute $py `
    -Argument "-u `"$SettleScript`" --refresh" -WorkingDirectory $ScriptDir

$settleTrigger = New-ScheduledTaskTrigger -Daily -At "05:30"

Register-ScheduledTask -TaskName $SettleTaskName -Action $settleAction `
    -Trigger $settleTrigger -Settings $settings `
    -Description "Joins logged odds to match results and reports CLV/ROI" -Force

# Roster sync updates current team squads, transfers, and injury return statuses
# from FotMob once a day early morning so that player prop filters stay pristine.
$rosterAction = New-ScheduledTaskAction -Execute $py `
    -Argument "-u `"$RosterScript`" sync" -WorkingDirectory $RootDir

$rosterTrigger = New-ScheduledTaskTrigger -Daily -At "04:00"

Register-ScheduledTask -TaskName $RosterTaskName -Action $rosterAction `
    -Trigger $rosterTrigger -Settings $settings `
    -Description "Syncs verified active team rosters and injuries from FotMob" -Force

Write-Host ""
Write-Host "Installed '$TaskName' - runs every 3 hours."
Write-Host "Installed '$SettleTaskName' - runs daily at 05:30."
Write-Host "Installed '$RosterTaskName' - runs daily at 04:00."
Write-Host "  check:    schtasks /Query /TN $TaskName"
Write-Host "  run now:  schtasks /Run   /TN $RosterTaskName"
Write-Host "  remove:   powershell -ExecutionPolicy Bypass -File install_task.ps1 -Uninstall"
