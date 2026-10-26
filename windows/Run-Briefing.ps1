<#
.SYNOPSIS
    Task Scheduler entry point: run one briefing edition through the conductor.

.DESCRIPTION
    Replaces Invoke-ClaudeTask.ps1 + SKILL.md for the briefings. The conductor (Python) does the
    orchestration; this script only prepares the environment, runs it, logs, and never fails
    silently. It is safe to launch repeatedly: the conductor is resumable and idempotent (it exits
    immediately once today's briefing is delivered), which is how the repetition trigger retries
    after a usage-limit deferral.

    Exit codes (from the conductor): 0 delivered, 10 delivered degraded, 75 deferred (limit; retry
    later), 20 failed (an alert has been written/emailed), 30 disabled, 2 usage error.

.PARAMETER Edition
    animal | daily
.PARAMETER Finish
    Watchdog mode: do not draft anything; assemble and deliver whatever exists (>= minimum articles).
.PARAMETER Diagnose
    Print the saved run state and exit.
.PARAMETER DryRun
    Run every stage except the delivery email.
.PARAMETER WhatIf
    Show the resolved command and environment without running anything.

.NOTES
    UNTESTED on the target machine (written from the cloud). Run once with -WhatIf, then with
    -DryRun -Edition animal before enabling the scheduled task.
#>
[CmdletBinding()]
param(
    [ValidateSet('animal', 'daily')]
    [string]$Edition = 'animal',
    [string]$Date = '',
    [switch]$Finish,
    [switch]$Diagnose,
    [switch]$DryRun,
    [switch]$NoDeliver,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$home_ = $env:USERPROFILE
$briefingsRoot = Join-Path $home_ 'Documents\Daily-Briefings'
$shared        = Join-Path $briefingsRoot 'pipeline-shared'
$scriptsDir    = if ($env:BRIEFING_SCRIPTS_DIR) { $env:BRIEFING_SCRIPTS_DIR } else { Join-Path $home_ '.claude\scheduled-tasks\daily-intelligence-briefing\scripts' }
$conductor     = Join-Path $shared 'conductor_core.py'
$here          = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir        = Join-Path $briefingsRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

foreach ($required in @($conductor, $scriptsDir)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required (run the 04:30 pipeline sync first, or fix BRIEFING_SCRIPTS_DIR)" }
}

# --- environment for the conductor and the stage scripts it launches --------------------------------
$env:BRIEFING_EDITION            = $Edition
$env:DAILY_BRIEFING_SHARED_ROOT  = $shared
$env:DAILY_BRIEFING_SCRIPTS_DIR  = $scriptsDir
$env:PYTHONIOENCODING            = 'utf-8'
$env:PYTHONUTF8                  = '1'

# Resolve claude.exe the way Invoke-ClaudeTask.ps1 does (the scheduled-task PATH is thinner than a shell's).
$nativeRoot = Join-Path $env:APPDATA 'Claude\claude-code'
$nativeLatest = if (Test-Path $nativeRoot) {
    Get-ChildItem $nativeRoot -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } | Select-Object -Last 1
} else { $null }
$claudeOnPath = Get-Command claude.exe -ErrorAction SilentlyContinue | Select-Object -First 1
$claudeCandidates = @(
    $(if ($claudeOnPath) { $claudeOnPath.Path }),
    "$env:USERPROFILE\bin\claude.exe",
    $(if ($nativeLatest) { Join-Path $nativeLatest.FullName 'claude.exe' }),
    "$env:USERPROFILE\.local\bin\claude.exe",
    "$env:LOCALAPPDATA\Microsoft\WinGet\Links\claude.exe"
)
$claudeExe = $claudeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $claudeExe) { throw "claude.exe not found. Checked: $($claudeCandidates -join ', ')" }
$env:BRIEFING_CLAUDE_EXE = $claudeExe
# `opus` resolves through the *User* environment variable; scheduled tasks do not always inherit it.
foreach ($name in 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL') {
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($value) { Set-Item -Path "Env:$name" -Value $value }
}

# Delivery goes through the one authorised path: PDF -> scp -> aiserver deliver_core.py (SMTP app password).
$deliverScript = Join-Path $here 'deliver-edition-via-aiserver.ps1'
$alertScript   = Join-Path $here 'send-alert-via-aiserver.ps1'
$env:BRIEFING_DELIVER_COMMAND = (@('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $deliverScript,
        '-Date', '{date}', '-Edition', '{edition}', '-PdfPath', '{pdf}', '-BodyTextFile', '{body_text}',
        '-BodyHtmlFile', '{body_html}', '-Subject', '{subject}', '-Recipient', '{recipients}') | ConvertTo-Json -Compress)
$env:BRIEFING_ALERT_COMMAND = (@('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $alertScript,
        '-Subject', '{subject}', '-BodyFile', '{body_file}') | ConvertTo-Json -Compress)

# Recipients live outside git. One file per edition, comma- or newline-separated addresses.
$recipientFile = Join-Path $home_ ".briefing\$Edition-recipients.txt"
if (Test-Path -LiteralPath $recipientFile) {
    $addresses = (Get-Content -LiteralPath $recipientFile -Raw) -split '[,;\r\n]+' | Where-Object { $_ -match '@' }
    if ($Edition -eq 'animal') { $env:ANIMAL_BRIEFING_RECIPIENTS = ($addresses -join ',') }
}

$argsList = @($conductor, '--edition', $Edition, '--scripts-dir', $scriptsDir)
if ($Date)      { $argsList += @('--date', $Date) }
if ($Finish)    { $argsList += '--finish' }
if ($Diagnose)  { $argsList += '--diagnose' }
if ($DryRun)    { $argsList += '--dry-run' }
if ($NoDeliver) { $argsList += '--no-deliver' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log   = Join-Path $logDir "briefing-$Edition-$stamp.log"

if ($WhatIf) {
    Write-Host "Would run: py -3 $($argsList -join ' ')"
    Write-Host "claude   : $claudeExe"
    Write-Host "Log file : $log"
    Write-Host "Recipients file present: $(Test-Path -LiteralPath $recipientFile)"
    exit 0
}

# --- run, tee to a log, never fail silently ---------------------------------------------------------
"[$(Get-Date -Format o)] start edition=$Edition finish=$Finish dryrun=$DryRun" | Tee-Object -FilePath $log
# Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'; the conductor logs to
# stderr too, so run the native command with 'Continue' and judge success by its exit code.
$ErrorActionPreference = 'Continue'
& py -3 @argsList 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
"[$(Get-Date -Format o)] exit=$code" | Tee-Object -FilePath $log -Append

$summary = [ordered]@{ edition = $Edition; finished_at = (Get-Date -Format o); exit_code = $code; log = $log }
$summary | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDir "last-run-$Edition.json") -Encoding UTF8

# Housekeeping: keep a month of logs.
Get-ChildItem -LiteralPath $logDir -Filter 'briefing-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue

exit $code
