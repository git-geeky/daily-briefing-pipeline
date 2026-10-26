<#
.SYNOPSIS
    Task Scheduler entry point: run one briefing edition through the conductor.

.DESCRIPTION
    Replaces Invoke-ClaudeTask.ps1 + SKILL.md for the briefings. The conductor (Python) does the
    orchestration; this script only prepares the environment, runs it, logs, and never fails
    silently: anything that goes wrong *here* (missing file, python not found, an exit code the
    conductor does not define) writes an alert file and tries the alert email too. It is safe to
    launch repeatedly: the conductor is resumable and idempotent (it exits immediately once today's
    briefing is delivered, and exits 76 while another run is active), which is how the repetition
    trigger retries after a usage-limit deferral.

    Exit codes (from the conductor): 0 delivered (also: already delivered), 10 delivered degraded,
    75 deferred (limit; retry later), 76 another run is active, 20 failed (an alert has been
    written/emailed), 30 disabled (alerted), 2 usage error. This launcher adds 21 for its own failures.

.PARAMETER Edition
    animal | daily
.PARAMETER Date
    Run (or resume) a specific briefing date, YYYY-MM-DD. Default: today in the edition's time zone.
.PARAMETER Finish
    Watchdog mode: do not draft anything; assemble and deliver whatever exists (>= minimum articles).
.PARAMETER Diagnose
    Print the saved run state and exit (writes no log and does not touch last-run-<edition>.json).
.PARAMETER DryRun
    Run every stage except the model limits probe and the delivery email.
.PARAMETER NoDeliver
    Real run that builds the PDF but sends nothing.
.PARAMETER NoDeadline
    Ignore the edition's local deadline for this run (manual runs started late in the day would otherwise stop drafting at once).
.PARAMETER WhatIf
    Show the resolved command and environment without running anything (creates nothing).

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
    [switch]$NoDeadline,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)       # no BOM: Windows PowerShell 5.1 would otherwise write UTF-16/BOM files
try { [Console]::OutputEncoding = $utf8 } catch { }

$home_         = $env:USERPROFILE
$briefingsRoot = Join-Path $home_ 'Documents\Daily-Briefings'
$editionRoot   = if ($Edition -eq 'animal') { Join-Path $briefingsRoot 'animal' } else { $briefingsRoot }
$alertsDir     = Join-Path $editionRoot 'generated\alerts'
$shared        = Join-Path $briefingsRoot 'pipeline-shared'
$scriptsDir    = if ($env:BRIEFING_SCRIPTS_DIR) { $env:BRIEFING_SCRIPTS_DIR } else { Join-Path $home_ '.claude\scheduled-tasks\daily-intelligence-briefing\scripts' }
$conductor     = Join-Path $shared 'conductor_core.py'
$here          = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir        = Join-Path $briefingsRoot 'logs'
$deliverScript = Join-Path $here 'deliver-edition-via-aiserver.ps1'
$alertScript   = Join-Path $here 'send-alert-via-aiserver.ps1'
$log           = Join-Path $logDir ("briefing-$Edition-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Write-Log([string]$Line) {
    Write-Host $Line
    if (-not $Diagnose) {
        try { [IO.File]::AppendAllText($log, $Line + "`r`n", $utf8) } catch { }
    }
}

function Send-LauncherAlert([string]$Key, [string]$Subject, [string]$Body) {
    # Alert file first (always visible on disk), email second (best effort: it needs the same aiserver path as delivery).
    try {
        New-Item -ItemType Directory -Force -Path $alertsDir | Out-Null
        $file = Join-Path $alertsDir ("ALERT-{0}-{1}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $Key)
        [IO.File]::WriteAllText($file, "$Subject`r`n`r`n$Body`r`n", $utf8)
        if (Test-Path -LiteralPath $alertScript) {
            $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $alertScript -Subject $Subject -BodyFile $file *> $null
            $ErrorActionPreference = $previous
        }
    } catch { }
}

try {
    foreach ($required in @($conductor, $scriptsDir)) {
        if (-not (Test-Path -LiteralPath $required)) { throw "Required path not found: $required (update pipeline-shared from the repository, or fix BRIEFING_SCRIPTS_DIR)" }
    }
    $python = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $python) { throw "The Python launcher (py.exe) was not found on PATH for this task's user" }

    # --- environment for the conductor and the stage scripts it launches ----------------------------------
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
    $env:BRIEFING_DELIVER_COMMAND = (@('powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $deliverScript,
            '-Date', '{date}', '-Edition', '{edition}', '-PdfPath', '{pdf}', '-BodyTextFile', '{body_text}',
            '-BodyHtmlFile', '{body_html}', '-Subject', '{subject}', '-Recipient', '{recipients}', '-AlsoRecipient', '{extra_recipients}') | ConvertTo-Json -Compress)
    $env:BRIEFING_ALERT_COMMAND = (@('powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $alertScript,
            '-Subject', '{subject}', '-BodyFile', '{body_file}') | ConvertTo-Json -Compress)

    # Recipients live outside git, two optional files per edition (comma- or newline-separated). The environment variable
    # names must match the edition's conductor.delivery.recipients_env / extra_recipients_env.
    #   <edition>-recipients.txt        REPLACES the delivery config's default recipient (use only to change who the owner is)
    #   <edition>-extra-recipients.txt  is ADDED to the default recipient, so extending the list can never drop the owner
    $recipientFile = Join-Path $home_ ".briefing\$Edition-recipients.txt"
    $extraFile     = Join-Path $home_ ".briefing\$Edition-extra-recipients.txt"
    $prefix        = if ($Edition -eq 'animal') { 'ANIMAL_BRIEFING' } else { 'DAILY_BRIEFING' }
    foreach ($pair in @(@($recipientFile, "${prefix}_RECIPIENTS"), @($extraFile, "${prefix}_EXTRA_RECIPIENTS"))) {
        if (Test-Path -LiteralPath $pair[0]) {
            $addresses = (Get-Content -LiteralPath $pair[0] -Raw) -split '[,;\r\n]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '@' }
            Set-Item -Path ("Env:" + $pair[1]) -Value ($addresses -join ',')
        }
    }

    $argsList = @('-3', $conductor, '--edition', $Edition, '--scripts-dir', $scriptsDir)
    if ($Date)      { $argsList += @('--date', $Date) }
    if ($Finish)    { $argsList += '--finish' }
    if ($Diagnose)  { $argsList += '--diagnose' }
    if ($DryRun)    { $argsList += '--dry-run' }
    if ($NoDeliver) { $argsList += '--no-deliver' }
    if ($NoDeadline) { $argsList += @('--deadline', 'none') }

    if ($WhatIf) {
        Write-Host "Would run: py $($argsList -join ' ')"
        Write-Host "claude   : $claudeExe"
        Write-Host "Log file : $log"
        Write-Host "Recipients file present: $(Test-Path -LiteralPath $recipientFile); extra recipients file present: $(Test-Path -LiteralPath $extraFile)"
        exit 0
    }

    if (-not $Diagnose) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

    # --- run, log, never fail silently ----------------------------------------------------------------------
    $revision = 'unknown'
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $revision = (& git -C $shared rev-parse --short HEAD 2>$null) } catch { }
    $ErrorActionPreference = $previous
    Write-Log "[$(Get-Date -Format o)] start edition=$Edition finish=$Finish dryrun=$DryRun date=$Date pipeline=$revision"

    # Windows PowerShell 5.1 turns native stderr into terminating errors under 'Stop'; the conductor logs to
    # stderr too, so run the native command with 'Continue' and judge success by its exit code alone.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = $null
    & py @argsList 2>&1 | ForEach-Object { Write-Log "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($null -eq $code) { $code = 127 }                     # py never ran
    Write-Log "[$(Get-Date -Format o)] exit=$code"

    if ($Diagnose) { exit $code }

    $known = @(0, 10, 20, 30, 75, 76)
    if ($known -notcontains $code) {
        Send-LauncherAlert "unexpected-exit-$code" "Briefing ($Edition): the conductor exited with unexpected code $code" `
            "The $Edition conductor ended with exit code $code, which it does not define (usually Python crashing at start-up or the launcher being killed).`r`nLog: $log"
    }

    $summary = [ordered]@{ edition = $Edition; finished_at = (Get-Date -Format o); exit_code = $code; log = $log }
    [IO.File]::WriteAllText((Join-Path $logDir "last-run-$Edition.json"), ($summary | ConvertTo-Json), $utf8)

    # Housekeeping: keep a month of logs.
    Get-ChildItem -LiteralPath $logDir -Filter 'briefing-*.log' | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force -ErrorAction SilentlyContinue

    exit $code
}
catch {
    $message = "$($_.Exception.Message)`r`n$($_.ScriptStackTrace)"
    Write-Log "[$(Get-Date -Format o)] LAUNCHER FAILURE: $message"
    Send-LauncherAlert 'launcher-failed' "Briefing ($Edition): the launcher failed before the conductor could run" `
        "$message`r`nThe scheduled run did not start. Fix the cause and the next repetition will pick up."
    exit 21
}
