# register_overnight.ps1 — create the Windows Task Scheduler entry
# "Flowstate Overnight Optimizer": daily 19:00, venv python running
# scripts/overnight_batch.py, working directory = repo root.
#
# Run from the MAIN checkout (never a worktree), elevated not required:
#   powershell -ExecutionPolicy Bypass -File scripts\register_overnight.ps1
# Remove with scripts\unregister_overnight.ps1.

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\overnight_batch.py"
$TaskName = "Flowstate Overnight Optimizer"

if (-not (Test-Path $Python)) { throw "venv python not found: $Python" }
if (-not (Test-Path $Script)) { throw "batch script not found: $Script" }

$Action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At 19:00
# Battery/idle settings: the box is meant to stay on all night; never stop
# the batch for idle-end, allow start on batteries, and give it a hard
# 14h ceiling so a wedged run cannot survive into the workday.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 14) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action `
    -Trigger $Trigger -Settings $Settings `
    -Description ("Nightly Flowstate solve portfolio: leaderboard + brief in " +
                  "data/optimizer, publishes overnight_best / " +
                  "overnight_runner_up sandbox versions. " +
                  "See scripts/overnight_batch.py.") -Force | Out-Null

Write-Host "Registered '$TaskName' (daily 19:00)."
Write-Host "  python : $Python"
Write-Host "  script : $Script"
Write-Host "  workdir: $RepoRoot"
