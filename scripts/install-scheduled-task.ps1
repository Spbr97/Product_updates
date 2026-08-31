param([string]$TaskName = "Product Updates Monitor")

$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project ".venv\Scripts\python.exe"
$config = Join-Path $project "config.yaml"
if (-not (Test-Path -LiteralPath $python)) { throw "Virtual environment not found. Create it with: python -m venv .venv" }

$action = New-ScheduledTaskAction -Execute $python -Argument "-m product_updates.cli run --config `"$config`"" -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) -ExecutionTimeLimit (New-TimeSpan -Days 365)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Checks configured product prices and sends change alerts." -Force
Write-Host "Installed '$TaskName'. It will start at your next sign-in."
