# Octopus backend hard-restart  (Windows / PowerShell 5.1-safe, ASCII-only)
#
# Why hard restart: uvicorn --reload on Windows often fails to fully replace the
# old worker, leaving stale code (404 on new routes, edits not taking effect).
#
# This script:
#   1. kills ONLY the process listening on port 8000 (+ its children).
#      It does NOT kill every python.exe -- ComfyUI / other python stay alive.
#   2. waits for the port to free.
#   3. clears __pycache__ (stale bytecode is the #1 "my edit did nothing" cause).
#   4. starts a single fresh uvicorn (no --reload by default = stable).
#
# Usage (run from the repo root):
#   .\restart_backend.ps1            # no --reload (default, stable)
#   .\restart_backend.ps1 -Reload    # opt in to --reload
#
# NOTE: keep this file ASCII-only. PowerShell 5.1 decodes .ps1 as the system
# ANSI codepage (GBK on zh-CN Windows); non-ASCII bytes can break parsing.

param([switch]$Reload)

$ErrorActionPreference = 'Continue'
$root       = $PSScriptRoot
$backendDir = Join-Path $root 'backend'
$logFile    = Join-Path $root 'backend.log'

Write-Host ''
Write-Host 'Octopus backend hard restart...' -ForegroundColor Cyan

# --- Step 1: kill ONLY what listens on :8000 (targeted, + child procs) ---------
$pids = @(
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
)
if ($pids.Count -gt 0) {
    Write-Host "[1/4] Killing PID(s) on :8000 -> $($pids -join ', ')" -ForegroundColor Yellow
    foreach ($procId in $pids) { taskkill /T /F /PID $procId 2>&1 | Out-Null }
} else {
    Write-Host '[1/4] Nothing is listening on :8000' -ForegroundColor Gray
}

# --- Step 2: wait for the port to be released (max ~7s) ------------------------
Write-Host '[2/4] Waiting for :8000 to free...' -ForegroundColor Yellow
$released = $false
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Milliseconds 700
    if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
        $released = $true; break
    }
}
if (-not $released) { Write-Host '    WARN: :8000 still busy after wait' -ForegroundColor Red }

# --- Step 3: clear __pycache__ ------------------------------------------------
Write-Host '[3/4] Clearing __pycache__...' -ForegroundColor Yellow
Get-ChildItem -Path (Join-Path $backendDir 'app') -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# --- Step 4: start uvicorn ----------------------------------------------------
# The bundled venv is a Linux ELF (created via git-bash) and cannot run on
# Windows; deps are installed in the system python's site-packages, so fall
# back to system python.
Write-Host '[4/4] Starting uvicorn...' -ForegroundColor Yellow
$py = $null
$winVenv = Join-Path $backendDir 'venv\Scripts\python.exe'
if (Test-Path $winVenv) {
    try { & $winVenv --version 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $winVenv } } catch {}
}
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) {
    Write-Host '    ERROR: no usable python found on PATH' -ForegroundColor Red
    exit 1
}

$uvArgs = @('-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8000')
if ($Reload) { $uvArgs += '--reload' }

$proc = Start-Process -FilePath $py -ArgumentList $uvArgs -WorkingDirectory $backendDir `
    -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err" -PassThru -WindowStyle Hidden
Write-Host "    uvicorn started (PID $($proc.Id)); logs: $logFile" -ForegroundColor Green

# --- readiness check ----------------------------------------------------------
# Primary signal: uvicorn accepting TCP on :8000 (reliable, no proxy/DNS).
# Startup is actually fast (~1-2s); the flaky part is Invoke-RestMethod inside a
# detached shell (proxy/session quirks), so HTTP /health is only a best-effort
# confirmation and never the thing that fails the script.
Write-Host 'Waiting for backend to come up on :8000...' -ForegroundColor Cyan
$listening = $false
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
        $listening = $true
        try {
            $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2 -ErrorAction Stop
            if ($r.status -eq 'ok') { $healthy = $true }
        } catch {}
        if ($healthy) { break }
        if ($i -ge 4) { break }   # port up for a few seconds -> good enough
    }
}
if ($healthy) {
    Write-Host "READY -> http://localhost:8000  (health OK, ~$($i)s)" -ForegroundColor Green
} elseif ($listening) {
    Write-Host "STARTED -> :8000 is listening (PID $($proc.Id)); health ping inconclusive but backend is up. Check backend.log.err if issues." -ForegroundColor Yellow
} else {
    Write-Host 'ERROR: backend did not open :8000 within 60s; check backend.log.err' -ForegroundColor Red
    exit 1
}
