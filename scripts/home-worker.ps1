# Runs only the background worker on this computer, for the "home computer" setup: your PC has a normal
# home address that YouTube does not block, so it takes the jobs made from YouTube links while the
# hosted server takes the rest. It needs backend\.env with the same MONGODB_URI, BLOMP_* and
# APP_SECRET_KEY as the server, and CLIPPER_WORKER_ACCEPTS=links. See docs\DEPLOY_RENDER.md.
param([switch]$Visible)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Python environment not found. See README.md, section 'First-time setup'." }
if (-not (Test-Path (Join-Path $backend ".env"))) { throw "backend\.env is missing. Copy .env.example and fill it in." }

$style = if ($Visible) { "Normal" } else { "Hidden" }
Start-Process -FilePath $python -WorkingDirectory $backend -WindowStyle $style `
    -ArgumentList "-m", "app.worker" `
    -RedirectStandardOutput (Join-Path $root "data\home-worker.log") `
    -RedirectStandardError (Join-Path $root "data\home-worker-errors.log")
Write-Host "Home worker started. It takes YouTube-link jobs while this computer is on."
Write-Host "Log: data\home-worker.log   Stop it from Task Manager (python.exe) or by signing out."
