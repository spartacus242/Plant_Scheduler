# unregister_overnight.ps1 — remove the "Flowstate Overnight Optimizer"
# Task Scheduler entry created by scripts\register_overnight.ps1.
#
#   powershell -ExecutionPolicy Bypass -File scripts\unregister_overnight.ps1

$ErrorActionPreference = "Stop"
$TaskName = "Flowstate Overnight Optimizer"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "'$TaskName' is not registered — nothing to do."
} else {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered '$TaskName'."
}
