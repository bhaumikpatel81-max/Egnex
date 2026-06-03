# ============================================================
# Egnex — One Click Start
# Run this script to start (or restart) the full app stack.
# It handles first-run setup automatically.
# ============================================================

param(
    [switch]$Rebuild,   # force image rebuild
    [switch]$Fresh      # wipe DB and start fresh (WARNING: deletes all data)
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "  Egnex — One Click Hire" -ForegroundColor White
Write-Host "  Starting development stack..." -ForegroundColor Gray
Write-Host ""

Set-Location $Root

# ── Stop any existing containers ──────────────────────────────
Write-Host "[1/4] Checking existing containers..." -ForegroundColor Cyan
$running = docker ps --format "{{.Names}}" 2>$null | Where-Object { $_ -match "egnex" }
if ($running) {
    Write-Host "      Stopping existing Egnex containers..." -ForegroundColor Gray
    docker compose -f docker-compose.dev.yml stop 2>$null
}

# ── Fresh wipe (optional) ──────────────────────────────────────
if ($Fresh) {
    Write-Host "      Wiping database volume (fresh start)..." -ForegroundColor Yellow
    docker compose -f docker-compose.dev.yml down -v 2>$null
}

# ── Start (with optional rebuild) ─────────────────────────────
Write-Host "[2/4] Starting containers..." -ForegroundColor Cyan
if ($Rebuild -or $Fresh) {
    docker compose -f docker-compose.dev.yml up -d --build
} else {
    docker compose -f docker-compose.dev.yml up -d
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: docker compose failed. Is Docker Desktop running?" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# ── Wait for backend to be healthy ────────────────────────────
Write-Host "[3/4] Waiting for backend to be ready..." -ForegroundColor Cyan
$tries = 0
do {
    Start-Sleep -Seconds 2
    $tries++
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8080/api/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) { break }
    } catch {}
    Write-Host "      Still starting ($tries)..." -ForegroundColor Gray
} while ($tries -lt 15)

if ($tries -ge 15) {
    Write-Host "WARNING: Backend did not respond after 30 s. Check Docker Desktop." -ForegroundColor Yellow
} else {
    Write-Host "      Backend is up." -ForegroundColor Green
}

# ── Apply pending migrations ───────────────────────────────────
Write-Host "[4/4] Applying pending migrations..." -ForegroundColor Cyan
$migrations = Get-ChildItem "$Root\database\*.sql" | Sort-Object Name
foreach ($f in $migrations) {
    # Run every migration idempotently — all use IF NOT EXISTS or ON CONFLICT
    Get-Content $f.FullName | docker exec -i egnex-db-1 psql -U ochuser -d oneclickhire -q 2>$null
    Write-Host "      $($f.Name)" -ForegroundColor Gray
}

# ── Install pip packages ───────────────────────────────────────
Write-Host "      Installing Python packages..." -ForegroundColor Gray
docker exec egnex-backend-1 pip install pypdf python-docx -q 2>$null | Out-Null

# ── Done ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "  App is running at http://localhost:8080" -ForegroundColor Green
Write-Host ""
Write-Host "  Credentials (all passwords: Egnex@2026)" -ForegroundColor White
Write-Host "    TA Manager   : ta.manager@amnex.com" -ForegroundColor Gray
Write-Host "    Recruiter    : recruiter1@amnex.com" -ForegroundColor Gray
Write-Host "    Hiring Mgr   : hiring.manager@amnex.com" -ForegroundColor Gray
Write-Host ""
Write-Host "  Opening browser..." -ForegroundColor Cyan
Start-Process "http://localhost:8080"
Write-Host ""
