# Fetches today's GEX levels and puts the full levels.txt on the clipboard,
# ready to paste into the AIAS strategies' "GEX Levels Paste" input
# (the same paste works on all three charts).
$ErrorActionPreference = "Stop"
$url = "https://raw.githubusercontent.com/bereanthink-gif/gex-seeds/main/levels.txt"
$repoRoot = Split-Path -Parent $PSScriptRoot

$levels = $null
try {
    $levels = (Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 15).Content
    $source = "GitHub (Action-published)"
} catch {
    Write-Host "GitHub fetch failed ($($_.Exception.Message)) - computing locally from CBOE..." -ForegroundColor Yellow
    python (Join-Path $PSScriptRoot "update_seeds.py") --symbols QQQ,SPY,GLD --repo-name gex-seeds | Out-Host
    $levels = Get-Content (Join-Path $repoRoot "levels.txt") -Raw
    $source = "local compute (CBOE)"
}

Set-Clipboard -Value $levels
Write-Host ""
Write-Host "=== GEX LEVELS -> COPIED TO CLIPBOARD ===" -ForegroundColor Green
Write-Host "Source: $source"
Write-Host ""
Write-Host $levels
Write-Host "Paste (Ctrl+V) into the 'GEX Levels Paste' input on each chart - same paste for NQ, ES, and GC." -ForegroundColor Cyan
Write-Host ""
Read-Host "Press Enter to close"
