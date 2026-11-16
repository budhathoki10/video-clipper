# Starts Clipper in the background and opens the dashboard.
# The engine keeps running after you close the browser. Stop it with scripts\stop.ps1.
param([switch]$NoBrowser, [switch]$Rebuild)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
$port = 8765
$url = "http://127.0.0.1:$port"

if (-not (Test-Path $python)) {
    throw "Python environment not found. See README.md, section 'First-time setup'."
}
if (-not (Test-Path (Join-Path $backend ".env"))) {
    Copy-Item (Join-Path $backend ".env.example") (Join-Path $backend ".env")
    Write-Host "Created backend\.env. Add your API key there or in Settings."
}
if ($Rebuild -or -not (Test-Path (Join-Path $frontend "out\index.html"))) {
    Write-Host "Building the dashboard (first run only)..."
    Push-Location $frontend
    try {
        if (-not (Test-Path "node_modules")) { npm install --no-fund --no-audit }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Dashboard build failed." }
    } finally { Pop-Location }
}

$running = $false
try { $running = (Invoke-WebRequest "$url/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}

if (-not $running) {
    $data = Join-Path $root "data"
    New-Item -ItemType Directory -Force $data | Out-Null
    Start-Process -FilePath $python -WorkingDirectory $backend -WindowStyle Hidden `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$port", "--no-access-log" `
        -RedirectStandardOutput (Join-Path $data "server.log") `
        -RedirectStandardError (Join-Path $data "server-errors.log")
    for ($i = 0; $i -lt 40 -and -not $running; $i++) {
        Start-Sleep -Milliseconds 500
        try { $running = (Invoke-WebRequest "$url/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
    }
    if (-not $running) { throw "Clipper did not start. Check data\server-errors.log, or run it in the foreground (see README.md)." }
    Write-Host "Clipper is running in the background at $url"
} else {
    Write-Host "Clipper is already running at $url"
}
if (-not $NoBrowser) { Start-Process $url }
