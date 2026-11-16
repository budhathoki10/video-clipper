# Stops the Clipper dashboard server and the background worker.
# Unfinished jobs resume the next time Clipper starts.
$root = (Split-Path -Parent $PSScriptRoot)
$found = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "app\.main:app|app\.worker" -and
                   $_.ExecutablePath -like "$root*" }
if (-not $found) { Write-Host "Clipper is not running."; return }
foreach ($process in $found) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
# Recordings of live streams run as ffmpeg children of the worker.
Get-CimInstance Win32_Process -Filter "Name = 'ffmpeg.exe'" |
    Where-Object { $_.CommandLine -like "*$root*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Clipper stopped ($(@($found).Count) processes)."
