# Daily verified backup of the EXP-2026-11-B pilot database (NOT installed by default).
#
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_backup_task.ps1            # dry run
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_backup_task.ps1 -Install   # needs authorization
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_backup_task.ps1 -Uninstall
#
# Copies the live database with SQLite's online backup API (read-only source
# connection; the logger keeps running), verifies integrity and append-only
# row counts, publishes atomically with a sha256 manifest, keeps the newest
# $Keep backups. Refuses a target on the source's volume or inside Dropbox.

param([switch]$Install, [switch]$Uninstall,
      [string]$Source = "D:\soccerlab-research\expb\exp_b_odds.sqlite3",
      [string]$Dest = "$env:USERPROFILE\soccerlab-backups\expb",
      [int]$Keep = 14, [string]$At = "06:15")

$TaskName = "soccerlab-expb-backup"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Worktree = Split-Path -Parent $ScriptDir
if (@("balldontlie-odds-logger", "balldontlie-odds-settle", "balldontlie-roster-sync", "soccerlab-expb-odds-logger") -contains $TaskName) {
    throw "refusing: task name collides with an existing task"
}
if ($Uninstall) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Write-Host "Removed $TaskName."; exit 0 }

$py = Join-Path (Split-Path -Parent $Worktree) "api-football-data-research\var\venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction Stop).Source }
$args_ = "-u -m soccerlab expb backup --source `"$Source`" --dest `"$Dest`" --keep $Keep"
$action = New-ScheduledTaskAction -Execute $py -Argument $args_ -WorkingDirectory $Worktree
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew

Write-Host "Task:     $TaskName (daily at $At local, catches up if the PC was off)"
Write-Host "Runs:     $py $args_"
Write-Host "Code:     $Worktree"
Write-Host "Source:   $Source (read-only)"
Write-Host "Target:   $Dest (keep $Keep)"
if (-not $Install) { Write-Host ""; Write-Host "DRY RUN - nothing registered."; exit 0 }
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "EXP-2026-11-B verified daily database backup (read-only on the source)" | Out-Null
Write-Host "Registered $TaskName."
