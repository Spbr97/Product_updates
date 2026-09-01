<#
.SYNOPSIS
    Run the Product Tracker worker automatically on Windows.

.DESCRIPTION
    Registers a Scheduled Task that starts `product-tracker worker` at sign-in and
    restarts it if it exits. The worker keeps running and checks each product on its own
    interval, so this is the only thing that needs to survive a reboot.

    Only one worker should run against a database at a time: APScheduler's job store has
    no cross-process locking, so a second worker would run every job a second time. The
    task is registered per-user and replaces any existing registration, so re-running this
    script does not create a duplicate.

.PARAMETER TaskName
    Name of the scheduled task. Default: ProductTrackerWorker.

.PARAMETER Remove
    Unregister the task instead of creating it.

.EXAMPLE
    .\scripts\install-scheduled-task.ps1
    .\scripts\install-scheduled-task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ProductTrackerWorker",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $projectRoot ".venv\Scripts\product-tracker.exe"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if (-not (Test-Path $exe)) {
    throw "product-tracker.exe not found at $exe. Create the virtualenv and run: pip install -e `".[dev]`""
}

if (-not (Test-Path (Join-Path $projectRoot ".env"))) {
    Write-Warning "No .env in $projectRoot. The worker needs DATABASE_URL; copy .env.example to .env first."
}

# Settings are read from .env in the working directory, so the task must start there.
$action = New-ScheduledTaskAction -Execute $exe -Argument "worker" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # runs indefinitely; it is a daemon

# -Force replaces an existing registration rather than adding a second one.
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Product Tracker background worker: recurring price and availability checks." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "  Start now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Check:      Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Remove:     .\scripts\install-scheduled-task.ps1 -Remove"
