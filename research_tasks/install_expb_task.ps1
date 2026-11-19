# Installs the EXP-2026-11-B research odds logger as its OWN scheduled task.
#
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1             # dry run (default)
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Install    # register (needs authorization)
#   powershell -ExecutionPolicy Bypass -File research_tasks\install_expb_task.ps1 -Uninstall
#
# It never touches the legacy tasks (balldontlie-odds-logger, -settle,
# -roster-sync), the main checkout, telegram_config.json or paper.sqlite3.
# It runs `python -m soccerlab expb run` from the research worktree every
# 5 minutes; each run is lock-protected, budgeted to ~4 minutes of API work,
# writes only to the research store (`python -m soccerlab expb where`: var\expb\ or the
# external store set by `expb init-store`), and sends no alerts.

param([switch]$Install, [switch]$Uninstall)

$TaskName = "soccerlab-expb-odds-logger"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Worktree = Split-Path -Parent $ScriptDir
$Legacy = @("balldontlie-odds-logger", "balldontlie-odds-settle", "balldontlie-roster-sync")

if ($Legacy -contains $TaskName) { throw "refusing: task name collides with a legacy task" }
if (Test-Path (Join-Path $Worktree "odds_logger\seen.json")) {
    throw "refusing: $Worktree looks like the live production checkout (odds_logger\seen.json present)"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed $TaskName (legacy tasks untouched)."
    exit 0
}

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw "python not found on PATH" }
$venvPy = Join-Path $Worktree "var\venv\Scripts\python.exe"
if (Test-Path $venvPy) { $py = $venvPy }

Push-Location $Worktree
$store = & $py -m soccerlab expb where
$storeOk = ($LASTEXITCODE -eq 0)
Pop-Location
if (-not $storeOk) { throw "research store unavailable: $store (plug in the drive or run: python -m soccerlab expb init-store <dir>)" }

$action = New-ScheduledTaskAction -Execute $py -Argument "-u -m soccerlab expb run" -WorkingDirectory $Worktree
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 6) -MultipleInstances IgnoreNew

Write-Host "Task:        $TaskName"
Write-Host "Runs:        $py -u -m soccerlab expb run"
Write-Host "Working dir: $Worktree"
Write-Host "Schedule:    every 5 minutes, max 6 minutes per run, no overlapping instances"
Write-Host "Writes:      $store only (database, lock, downloads)"

if (-not $Install) {
    Write-Host ""
    Write-Host "DRY RUN - nothing registered. Re-run with -Install once activation is authorized."
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "EXP-2026-11-B research odds logger (logging only; no alerts, no picks)" | Out-Null
Write-Host "Registered $TaskName. Check: schtasks /Query /TN $TaskName ; monitor: python -m soccerlab expb monitor"
