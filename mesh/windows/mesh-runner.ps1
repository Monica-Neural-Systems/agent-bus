<#
.SYNOPSIS
  Mesh wire runner for OpenClaw (Windows host). Implements the same canonical
  task envelope as mesh/pipeline.py: nine invariant stages, idempotent ingest,
  per-agent dispatch, unavailable-not-fabricated collection, approval gates.

  The agent-bus repo is the shared state. This runner pulls, advances every
  live task one stage per pass, then pushes. Telegram intake is OPTIONAL;
  the watched-folder adapter and chat_upload (Owen side) cover intake alone.

.EXAMPLE
  .\mesh-runner.ps1 -Once              # single pass (testing)
  .\mesh-runner.ps1                    # loop forever, 30s per pass
#>
param(
  [string]$RepoPath = "$HOME\AI-Workspace\agent-bus",
  [string]$IntakeDir = "$HOME\AI-Workspace\drive-wire\00_intake",
  [string]$BotToken = "",
  [int]$CollectTimeoutMin = 15,
  [switch]$Once
)

$ErrorActionPreference = "Continue"
$MeshDir  = Join-Path $RepoPath "mesh"
$TasksDir = Join-Path $MeshDir "tasks"
$ArchDir  = Join-Path $MeshDir "archive"
$InboxDir = Join-Path $RepoPath "inbox"
$RegistryFile = Join-Path $MeshDir "registry.json"
$Agents = @("openclaw", "claude", "spark", "owen")
$AgentRoles = @{
  openclaw = "Windows host daemon: intake adapters, watched folders, Telegram alerts, local file ops."
  claude   = "Lead Reasoning: Fast Response v8 drafts (dial rules, <850/<280, text blocks, 3-line breakdown), business packages, epistemic challenges."
  spark    = "Editor-in-Chief: final editorial sign-off on ALL public text."
  owen     = "Web Scout + Distribution: research, verification, Buffer queueing (post-approval), execution, audit."
}

function Write-MeshLog($msg) {
  $ts = (Get-Date).ToUniversalTime().ToString("o")
  Write-Host "[$ts] $msg"
}

function Get-Sha1Hex([string]$text) {
  $bytes = [Text.Encoding]::UTF8.GetBytes($text)
  $sha = [Security.Cryptography.SHA1]::Create()
  $hash = $sha.ComputeHash($bytes)
  ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Get-FileSha1Hex([string]$path) {
  $sha = [Security.Cryptography.SHA1]::Create()
  $stream = [IO.File]::OpenRead($path)
  try { $hash = $sha.ComputeHash($stream) } finally { $stream.Close() }
  ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Get-TaskId([string]$adapter, [string]$evidenceKey) {
  $minute = (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmm")
  (Get-Sha1Hex "$adapter|$evidenceKey|$minute").Substring(0, 16)
}

function Read-Envelope([string]$taskDir) {
  $p = Join-Path $taskDir "envelope.json"
  Get-Content $p -Raw | ConvertFrom-Json -Depth 12
}

function Write-Envelope($env, [string]$taskDir) {
  $p = Join-Path $taskDir "envelope.json"
  $env | ConvertTo-Json -Depth 12 | Set-Content $p -Encoding UTF8
}

function Add-Audit($env, [string]$line, [string]$by = "openclaw") {
  $ts = (Get-Date).ToUniversalTime().ToString("o")
  $env.audit += "[$ts] [$by] $line"
}

function Set-Stage($env, [string]$stage, [string]$note = "") {
  $env.stage = $stage
  $env.stage_history += [pscustomobject]@{
    stage = $stage; at = (Get-Date).ToUniversalTime().ToString("o"); by = "openclaw"; note = $note
  }
  Add-Audit $env "stage -> $stage $(if ($note) { "($note)" })"
}

function Get-Registry() {
  if (Test-Path $RegistryFile) { Get-Content $RegistryFile -Raw | ConvertFrom-Json }
  else { [pscustomobject]@{ processed = @() } }
}

# ---------- INGEST ----------

function New-TaskFromFile([string]$filePath, [string]$adapter, [string]$intent, [string]$dial) {
  $sha = Get-FileSha1Hex $filePath
  $taskId = Get-TaskId $adapter "img:$sha"
  $taskDir = Join-Path $TasksDir $taskId
  $evDir = Join-Path $taskDir "evidence"
  New-Item -ItemType Directory -Force -Path (Join-Path $taskDir "results") | Out-Null
  New-Item -ItemType Directory -Force -Path $evDir | Out-Null
  $destName = "evidence_" + [IO.Path]::GetFileName($filePath)
  Copy-Item $filePath (Join-Path $evDir $destName) -Force
  $relImg = "mesh/tasks/$taskId/evidence/$destName"

  $dispatch = [pscustomobject]@{}
  foreach ($a in $Agents) {
    $dispatch | Add-Member -NotePropertyName $a -NotePropertyValue ([pscustomobject]@{
      role = $AgentRoles[$a]; status = "pending"; result_ref = $null; note = ""; updated_at = $null
    })
  }
  $approvals = [pscustomobject]@{}
  foreach ($g in @("spark_editorial", "michael_send", "michael_buffer", "michael_spend")) {
    $approvals | Add-Member -NotePropertyName $g -NotePropertyValue ([pscustomobject]@{
      status = "not_required"; by = $null; at = $null; text_sha1 = $null
    })
  }
  $env = [pscustomobject]@{
    envelope_version = "1.0"; task_id = $taskId
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    source_adapter = $adapter; task_type = "screenshot_rebuttal"
    user_intent = $intent; dial_command = $dial
    evidence = [pscustomobject]@{ image_path = $relImg; image_sha1 = $sha; text = $null; url = $null }
    stage = "INGEST"
    stage_history = @([pscustomobject]@{ stage = "INGEST"; at = (Get-Date).ToUniversalTime().ToString("o"); by = $adapter; note = "ingested" })
    dispatch = $dispatch; approvals = $approvals
    result = [pscustomobject]@{ synthesized = $null; delivery_channel = $null; delivered_at = $null }
    duplicate_of = $null
    audit = @("[$( (Get-Date).ToUniversalTime().ToString('o') )] [$adapter] ingested: $intent")
  }
  Write-Envelope $env $taskDir
  Write-MeshLog "ingested task $taskId from $adapter"
  return $taskId
}

function Invoke-WatchedFolderIntake() {
  if (-not (Test-Path $IntakeDir)) { return }
  $files = Get-ChildItem -Path $IntakeDir -Include *.png, *.jpg, *.jpeg -File -ErrorAction SilentlyContinue
  foreach ($f in $files) {
    # sidecar .txt with same basename carries intent/dial; else defaults
    $sidecar = Join-Path $IntakeDir ($f.BaseName + ".txt")
    $intent = "screenshot intake"; $dial = "AUTO_TRIAGE"
    if (Test-Path $sidecar) {
      $lines = Get-Content $sidecar
      if ($lines.Count -ge 1) { $intent = $lines[0] }
      if ($lines.Count -ge 2) { $dial = $lines[1] }
    }
    New-TaskFromFile $f.FullName "watched_folder" $intent $dial | Out-Null
    Move-Item $f.FullName (Join-Path $IntakeDir ("processed_" + $f.Name)) -Force
    if (Test-Path $sidecar) { Remove-Item $sidecar -Force }
  }
}

# ---------- STAGE ADVANCEMENT ----------

function Invoke-Normalize([string]$taskDir, $env) {
  $reg = Get-Registry
  if ($reg.processed -contains $env.task_id) {
    $env.duplicate_of = $env.task_id
    Add-Audit $env "NORMALIZE: duplicate of processed task, archiving immediately"
    Set-Stage $env "ARCHIVE" "duplicate"
  } else {
    if ($env.task_type -eq "screenshot_rebuttal") {
      $env.approvals.spark_editorial.status = "pending"
      Add-Audit $env "NORMALIZE: spark_editorial gate armed (public text task)"
    }
    Set-Stage $env "NORMALIZE" "evidence hashed, gates armed"
  }
  Write-Envelope $env $taskDir
}

function Invoke-Dispatch([string]$taskDir, $env) {
  foreach ($a in $Agents) {
    $slot = $env.dispatch.$a
    $ev = $env.evidence.image_path
    if (-not $ev) { $ev = $env.evidence.text }
    if (-not $ev) { $ev = $env.evidence.url }
    $body = "# Task $($env.task_id)`r`n`r`n**Type:** $($env.task_type)  `r`n**Intent:** $($env.user_intent)  `r`n**Dial:** $($env.dial_command)  `r`n**Your role:** $($slot.role)  `r`n`r`nEnvelope: ``mesh/tasks/$($env.task_id)/envelope.json``  `r`nEvidence: ``$ev```r`n`r`nWrite your result to ``mesh/tasks/$($env.task_id)/results/$a.json`` as ``{`"agent`": `"$a`", `"status`": `"done`", `"payload`": {...}, `"note`": `"..."`}``.`r`nIf you cannot act, do NOT write a fake result — the collector records you unavailable.`r`n"
    $agentInbox = Join-Path $InboxDir $a
    New-Item -ItemType Directory -Force -Path $agentInbox | Out-Null
    Set-Content (Join-Path $agentInbox "task-$($env.task_id).md") $body -Encoding UTF8
    $slot.note = "dispatched to inbox/$a/"
  }
  Set-Stage $env "DISPATCH" "fanned out to $($Agents.Count) agents"
  Set-Stage $env "COLLECT" "awaiting agent results"
  Write-Envelope $env $taskDir
}

function Invoke-Collect([string]$taskDir, $env) {
  $resultsDir = Join-Path $taskDir "results"
  $allSettled = $true
  foreach ($a in $Agents) {
    $slot = $env.dispatch.$a
    if ($slot.status -ne "pending") { continue }
    $rf = Join-Path $resultsDir "$a.json"
    if (Test-Path $rf) {
      $res = Get-Content $rf -Raw | ConvertFrom-Json
      $slot.status = $res.status
      $slot.result_ref = "mesh/tasks/$($env.task_id)/results/$a.json"
      $slot.note = $res.note
      $slot.updated_at = (Get-Date).ToUniversalTime().ToString("o")
      Add-Audit $env "COLLECT: $a -> $($slot.status)"
    } else {
      # timeout: task dir older than CollectTimeoutMin with no result
      $age = (Get-Date).ToUniversalTime() - [DateTime]::Parse($env.created_at).ToUniversalTime()
      if ($age.TotalMinutes -ge $CollectTimeoutMin) {
        $slot.status = "unavailable"
        $slot.note = "no result within ${CollectTimeoutMin} min — recorded, not fabricated"
        $slot.updated_at = (Get-Date).ToUniversalTime().ToString("o")
        Add-Audit $env "COLLECT: $a UNAVAILABLE (timeout)"
      } else {
        $allSettled = $false
      }
    }
  }
  Write-Envelope $env $taskDir
  if ($allSettled) {
    Set-Stage $env "SYNTHESIZE" "all agents settled"
    Write-Envelope $env $taskDir
    Write-MeshLog "task $($env.task_id) collected -> SYNTHESIZE"
  }
}

function Invoke-Synthesize([string]$taskDir, $env) {
  $resultsDir = Join-Path $taskDir "results"
  $inputs = @{}
  foreach ($a in $Agents) {
    $rf = Join-Path $resultsDir "$a.json"
    if (Test-Path $rf) { $inputs[$a] = (Get-Content $rf -Raw | ConvertFrom-Json) }
    else { $inputs[$a] = @{ status = $env.dispatch.$a.status } }
  }
  $synth = @{ task_id = $env.task_id; task_type = $env.task_type; inputs = $inputs }
  if ($env.task_type -eq "screenshot_rebuttal" -and $inputs.ContainsKey("claude") -and $inputs["claude"].payload) {
    $synth["primary_draft"] = $inputs["claude"].payload
    $ann = @{}
    foreach ($k in $inputs.Keys) { if ($k -ne "claude" -and $inputs[$k].payload) { $ann[$k] = $inputs[$k].payload } }
    $synth["annotations"] = $ann
  }
  $env.result.synthesized = $synth
  Set-Stage $env "SYNTHESIZE" "merged agent results (owen lane)"
  Write-Envelope $env $taskDir
}

function Invoke-Validate([string]$taskDir, $env) {
  $problems = @()
  if ($env.task_type -in @("screenshot_rebuttal", "outreach_draft", "business_package")) {
    if ($env.approvals.spark_editorial.status -ne "approved") { $problems += "spark_editorial not approved" }
  }
  if ($env.result.delivery_channel -eq "buffer" -and $env.approvals.michael_buffer.status -ne "approved") {
    $problems += "michael_buffer not approved"
  }
  if ($problems.Count -gt 0) {
    Add-Audit $env ("VALIDATE FAILED: " + ($problems -join "; "))
    Write-Envelope $env $taskDir
    Write-MeshLog "task $($env.task_id) VALIDATE FAILED: $($problems -join '; ')"
    return
  }
  Set-Stage $env "VALIDATE" "all gates pass"
  Write-Envelope $env $taskDir
}

function Invoke-Archive([string]$taskDir, $env) {
  New-Item -ItemType Directory -Force -Path $ArchDir | Out-Null
  $dst = Join-Path $ArchDir $env.task_id
  if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
  Move-Item $taskDir $dst -Force
  $reg = Get-Registry
  if ($reg.processed -notcontains $env.task_id) { $reg.processed += $env.task_id }
  $reg | ConvertTo-Json -Depth 4 | Set-Content $RegistryFile -Encoding UTF8
  # move inbox files to done/
  foreach ($a in $Agents) {
    $p = Join-Path $InboxDir "$a/task-$($env.task_id).md"
    if (Test-Path $p) {
      $done = Join-Path $InboxDir "$a/done"
      New-Item -ItemType Directory -Force -Path $done | Out-Null
      Move-Item $p (Join-Path $done "task-$($env.task_id).md") -Force
    }
  }
  Write-MeshLog "task $($env.task_id) archived"
}

function Invoke-AdvanceTask([string]$taskDir) {
  $env = Read-Envelope $taskDir
  switch ($env.stage) {
    "INGEST"    { Invoke-Normalize $taskDir $env }
    "NORMALIZE" { Invoke-Dispatch $taskDir $env }
    "COLLECT"   { Invoke-Collect $taskDir $env }
    "SYNTHESIZE" {
      Invoke-Synthesize $taskDir $env
      $env = Read-Envelope $taskDir
      Invoke-Validate $taskDir $env
    }
    "VALIDATE" {
      Set-Stage $env "AUTHORIZED_EXECUTION" "approved actions only"
      Write-Envelope $env $taskDir
    }
    "AUTHORIZED_EXECUTION" {
      # Delivery itself is performed by the owning agent's tools (Owen: Zapier Buffer;
      # OpenClaw: Telegram). The runner stages delivery.md and parks at DELIVER.
      $chan = $env.result.delivery_channel
      if (-not $chan) { $chan = "chat" }
      $env.result.delivery_channel = $chan
      $env.result.delivered_at = (Get-Date).ToUniversalTime().ToString("o")
      $synthJson = ($env.result.synthesized | ConvertTo-Json -Depth 8)
      if ($synthJson.Length -gt 4000) { $synthJson = $synthJson.Substring(0, 4000) }
      Set-Content (Join-Path $taskDir "delivery.md") "# Delivery $($env.task_id)`r`n`r`nchannel: $chan`r`n`r`n``````json`r`n$synthJson`r`n``````" -Encoding UTF8
      Set-Stage $env "DELIVER" "delivered via $chan"
      Write-Envelope $env $taskDir
    }
    "DELIVER"   { Invoke-Archive $taskDir $env }
  }
}

# ---------- MAIN ----------

function Invoke-GitSync([string]$action) {
  Push-Location $RepoPath
  try {
    if ($action -eq "pull") { git pull --ff-only 2>&1 | Out-Null }
    else { git add -A; git commit -m "mesh: wire sync $(Get-Date -Format 'yyyy-MM-dd HH:mm')" 2>&1 | Out-Null; git push 2>&1 | Out-Null }
  } finally { Pop-Location }
  Write-MeshLog "git $action done"
}

Write-MeshLog "mesh-runner starting (repo $RepoPath)"
New-Item -ItemType Directory -Force -Path $TasksDir | Out-Null
New-Item -ItemType Directory -Force -Path $IntakeDir | Out-Null

do {
  if (Test-Path (Join-Path $RepoPath ".git")) { Invoke-GitSync "pull" }

  Invoke-WatchedFolderIntake
  # Telegram intake is optional and disabled when no token is configured.
  # if ($BotToken) { Invoke-TelegramIntake }

  $live = Get-ChildItem -Path $TasksDir -Directory -ErrorAction SilentlyContinue
  foreach ($t in $live) {
    try { Invoke-AdvanceTask $t.FullName }
    catch { Write-MeshLog "task $($t.Name) error: $($_.Exception.Message)" }
  }

  if (Test-Path (Join-Path $RepoPath ".git")) { Invoke-GitSync "push" }
  if ($Once) { break }
  Start-Sleep -Seconds 30
} while ($true)

Write-MeshLog "mesh-runner pass complete"
