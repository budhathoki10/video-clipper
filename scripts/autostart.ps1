# Makes Clipper start in the background every time you sign in to Windows,
# so queued videos and live watches resume after a restart.
#   scripts\autostart.ps1            turn on
#   scripts\autostart.ps1 -Remove    turn off
param([switch]$Remove)

$name = "Clipper background engine"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Clipper no longer starts with Windows."
    return
}
$script = Join-Path $PSScriptRoot "start.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -NoBrowser"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Clipper will start in the background each time you sign in."
