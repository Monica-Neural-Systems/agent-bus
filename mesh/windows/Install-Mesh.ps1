<#
.SYNOPSIS
  One-time Mesh setup on the Windows host. Run once from an elevated-or-normal
  PowerShell prompt. Safe to re-run: it never deletes existing assets.

  Creates:
    AI-Workspace\drive-wire\00_intake\      (watched-folder intake adapter)
    AI-Workspace\agent-bus\                 (git clone of the shared wire)
    Startup entry                           (mesh-runner.ps1 on login)
#>
$ErrorActionPreference = "Stop"
$base = "$HOME\AI-Workspace"
$intake = "$base\drive-wire\00_intake"
$repo = "$base\agent-bus"

Write-Host "== Mesh one-time setup ==" -ForegroundColor Cyan

foreach ($d in @("$base\drive-wire\01_tasks_inbox", "$base\drive-wire\02_in_progress",
                 "$base\drive-wire\03_completed", "$base\drive-wire\04_dead_letter",
                 "$base\drive-wire\05_human_review", $intake)) {
  New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Host "[ok] drive-wire folders + 00_intake" -ForegroundColor Green

if (-not (Test-Path "$repo\.git")) {
  git clone https://github.com/Monica-Neural-Systems/agent-bus $repo
  Write-Host "[ok] cloned agent-bus" -ForegroundColor Green
} else {
  Write-Host "[ok] agent-bus already present (kept as-is)" -ForegroundColor Green
}

# Startup entry: runs mesh-runner.ps1 on login. Existing entry is replaced.
$runner = "$repo\mesh\windows\mesh-runner.ps1"
$startupBat = "$([Environment]::GetFolderPath('Startup'))\Mesh Wire Runner.bat"
"@echo off`npowershell -NoExit -ExecutionPolicy Bypass -File `"$runner`" -Once:0" | Set-Content -Path $startupBat
Write-Host "[ok] startup entry: $startupBat" -ForegroundColor Green

Write-Host ""
Write-Host "Drop a screenshot into: $intake" -ForegroundColor Yellow
Write-Host "Optional sidecar: <same-name>.txt with line1=intent, line2=dial (1-9 or AUTO_TRIAGE)" -ForegroundColor Yellow
Write-Host "Then run once to test: powershell -ExecutionPolicy Bypass -File `"$runner`" -Once" -ForegroundColor Yellow
