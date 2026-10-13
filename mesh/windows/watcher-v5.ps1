<#
.SYNOPSIS
  Telegram watcher v5 — self-diagnosing replacement for start-watcher.ps1.
  Every failure prints. Nothing is swallowed. Text messages get a reply.

  First run:  .\watcher-v5.ps1 -Setup
  Normal run: .\watcher-v5.ps1   (or -Once for a single pass)

  Config lives in watcher-config.json next to this script. The token is
  never printed.
#>
param([switch]$Setup, [switch]$Once)

$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$ConfigPath = Join-Path $Dir "watcher-config.json"
$IntakeDir = "$HOME\AI-Workspace\drive-wire\00_intake"

function Get-Config {
  if (Test-Path $ConfigPath) { return Get-Content $ConfigPath -Raw | ConvertFrom-Json }
  return [pscustomobject]@{ BotToken = ""; ChatId = "" }
}

function Invoke-BotApi([string]$token, [string]$method, [hashtable]$body = $null) {
  $url = "https://api.telegram.org/bot$token/$method"
  try {
    if ($body) { return Invoke-RestMethod -Uri $url -Method Post -Body $body -TimeoutSec 15 }
    return Invoke-RestMethod -Uri "$url" -TimeoutSec 15
  } catch {
    Write-Host "[ERROR] $method failed: $($_.Exception.Message)" -ForegroundColor Red
    return $null
  }
}

# ---------------- SETUP ----------------
if ($Setup) {
  Write-Host "== Telegram watcher setup ==" -ForegroundColor Cyan
  $token = Read-Host "Paste the bot token from @BotFather"
  $me = Invoke-BotApi $token "getMe"
  if (-not $me -or -not $me.ok) { Write-Host "Token rejected by Telegram. Get a fresh one from @BotFather." -ForegroundColor Red; exit 1 }
  Write-Host "[ok] token valid — bot: @$($me.result.username)" -ForegroundColor Green
  Write-Host ""
  Write-Host "Now open Telegram and send /start to @$($me.result.username), then press Enter here."
  Read-Host "Press Enter when done"
  $updates = Invoke-BotApi $token "getUpdates"
  $chatId = $null
  foreach ($u in $updates.result) {
    if ($u.message -and $u.message.chat -and $u.message.chat.id) { $chatId = $u.message.chat.id }
  }
  if (-not $chatId) { Write-Host "No messages found. Send /start to the bot first, then re-run -Setup." -ForegroundColor Red; exit 1 }
  @{ BotToken = $token; ChatId = "$chatId" } | ConvertTo-Json | Set-Content $ConfigPath -Encoding UTF8
  Write-Host "[ok] chat id $chatId saved to watcher-config.json" -ForegroundColor Green
  $sent = Invoke-BotApi $token "sendMessage" @{ chat_id = $chatId; text = "Watcher v5 setup complete. Send me a photo to test intake." }
  if ($sent -and $sent.ok) { Write-Host "[ok] test message delivered to your phone" -ForegroundColor Green }
  else { Write-Host "[FAIL] could not send test message" -ForegroundColor Red; exit 1 }
  exit 0
}

# ---------------- RUN ----------------
$cfg = Get-Config
if (-not $cfg.BotToken) { Write-Host "No config. Run .\watcher-v5.ps1 -Setup first." -ForegroundColor Red; exit 1 }
$token = $cfg.BotToken
$chatId = $cfg.ChatId

$me = Invoke-BotApi $token "getMe"
if (-not $me -or -not $me.ok) { Write-Host "[FATAL] token rejected. Re-run -Setup with a fresh token." -ForegroundColor Red; exit 1 }
Write-Host "[ok] connected as @$($me.result.username)" -ForegroundColor Green

New-Item -ItemType Directory -Force -Path $IntakeDir | Out-Null
$hello = Invoke-BotApi $token "sendMessage" @{ chat_id = $chatId; text = "Watcher v5 online on your laptop." }
if ($hello -and $hello.ok) { Write-Host "[ok] startup ping delivered" -ForegroundColor Green }
else { Write-Host "[WARN] startup ping failed — will keep running anyway" -ForegroundColor Yellow }

$lastUpdateId = 0
do {
  try {
    $updates = Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/getUpdates?offset=$($lastUpdateId + 1)&timeout=20" -TimeoutSec 30
  } catch {
    Write-Host "[WARN] poll failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
    continue
  }
  foreach ($up in $updates.result) {
    $lastUpdateId = $up.update_id
    $msg = $up.message
    if (-not $msg -or "$($msg.chat.id)" -ne "$chatId") { continue }
    $text = if ($msg.text) { $msg.text } else { $msg.caption }
    Write-Host "[msg] $text" -ForegroundColor Gray

    if ($msg.photo) {
      $fileId = $msg.photo[-1].file_id
      $fi = Invoke-BotApi $token "getFile" @{ file_id = $fileId }
      if ($fi -and $fi.ok) {
        $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $imgPath = Join-Path $IntakeDir "tg_$stamp.jpg"
        Invoke-WebRequest -Uri "https://api.telegram.org/file/bot$token/$($fi.result.file_path)" -OutFile $imgPath
        $sidecar = Join-Path $IntakeDir "tg_$stamp.txt"
        Set-Content $sidecar ("telegram photo" + "`r`n" + $(if ($text) { $text } else { "AUTO_TRIAGE" })) -Encoding UTF8
        Write-Host "[photo] saved $imgPath" -ForegroundColor Green
        Invoke-BotApi $token "sendMessage" @{ chat_id = $chatId; text = "Photo received, queued for the wire." } | Out-Null
      } else {
        Write-Host "[ERROR] getFile failed for photo" -ForegroundColor Red
      }
    } elseif ($text) {
      Invoke-BotApi $token "sendMessage" @{ chat_id = $chatId; text = "Got it: $text" } | Out-Null
    }
  }
  if ($Once) { break }
} while ($true)
Write-Host "pass complete"
