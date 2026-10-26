<#
.SYNOPSIS
    Deliver an edition's PDF through the aiserver SMTP path (generalisation of deliver-via-aiserver.ps1).

.DESCRIPTION
    Keeps the one authorised delivery path (scp to aiserver, deliver_core.py there sends via the
    Gmail app password). Differences from the daily-only script: edition-aware paths, optional
    covering text/HTML body, comma-separated recipients, and no re-validation here (the conductor
    just validated and built); the run-report delivery gate is still enforced.

    Requires on aiserver: the updated deliver_core.py (adds --edition, --body-text-file,
    --body-html-file, multi-recipient and receipts) and an "editions.animal" block in its
    delivery-config.json (subject template; optional recipients).

.NOTES
    UNTESTED on the target machine (written from the cloud). Do not use Gmail MCP for delivery.
#>
param(
    [Parameter(Mandatory = $true)][string]$Date,
    [Parameter(Mandatory = $true)][ValidateSet('animal', 'daily')][string]$Edition,
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [string]$BodyTextFile = '',
    [string]$BodyHtmlFile = '',
    [string]$Subject = '',
    [string]$Recipient = '',
    [string]$AlsoRecipient = ''
)

Set-StrictMode -Version Latest
# 'Continue', not 'Stop': under Windows PowerShell 5.1 any text a native tool (scp, ssh, py) writes to stderr becomes a
# terminating error and would abort a delivery that actually succeeded. Every native call below checks $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

$briefingsRoot = Join-Path $env:USERPROFILE 'Documents\Daily-Briefings'
$scriptsDir = if ($env:DAILY_BRIEFING_SCRIPTS_DIR) { $env:DAILY_BRIEFING_SCRIPTS_DIR } else { Join-Path $env:USERPROFILE '.claude\scheduled-tasks\daily-intelligence-briefing\scripts' }
$scriptsDir = $scriptsDir.TrimEnd('\')   # a trailing backslash would break the raw Python string literals below
$stem = if ($Edition -eq 'animal') { 'claude-animal-briefing' } else { 'claude-daily-briefing' }

if (-not (Test-Path -LiteralPath $PdfPath)) { throw "PDF not found: $PdfPath" }
$resolvedPdf = (Resolve-Path $PdfPath).Path
$pdfLeaf = Split-Path $resolvedPdf -Leaf
# The daily edition may ship the combined PDF (animal section first): claude-daily-briefing-<date>-combined.pdf.
if (@("$stem-$Date.pdf", "$stem-$Date-combined.pdf") -notcontains $pdfLeaf) { throw "Unexpected PDF name '$pdfLeaf'; expected $stem-$Date.pdf (or $stem-$Date-combined.pdf)" }

# Stage-order gate: validation passed, PDF built, visual QA done, source audit complete.
$env:BRIEFING_EDITION = $Edition
$gate = @"
import sys
sys.path.insert(0, r"$scriptsDir")
import run_reporting
report = run_reporting.load_run_report(r"$Date")
allowed, reason = run_reporting.stage_prerequisites_satisfied(report, "delivery")
if not allowed:
    run_reporting.update_run_report(r"$Date", delivery_status="blocked", delivery_blocked_reason=reason)
    raise SystemExit(reason)
"@
$gate | py -3 -
if ($LASTEXITCODE -ne 0) { throw 'Delivery blocked by the stage-order gate (see the run report for the reason).' }

if (-not (Get-Command Invoke-RemoteBash -ErrorAction SilentlyContinue)) {
    $toolsPath = Join-Path $HOME 'bin\codex-ssh-tools.ps1'
    if (Test-Path -LiteralPath $toolsPath) { . $toolsPath }
}
$invokeRemoteBash = Get-Command Invoke-RemoteBash -ErrorAction Stop
$transport = Get-Command Get-RemoteSshTransport -ErrorAction Stop | ForEach-Object { & $_ }

function Copy-ToAiserver([string]$LocalPath, [string]$RemotePath) {
    $dir  = Split-Path $LocalPath -Parent
    $leaf = Split-Path $LocalPath -Leaf
    if ($transport -eq 'windows') {
        Push-Location $dir
        try {
            & scp ".\$leaf" "aiserver:$RemotePath"           # relative path: Windows scp misreads C:\ as host:path
            if ($LASTEXITCODE -ne 0) { throw "scp failed for $leaf (exit $LASTEXITCODE)" }
        } finally { Pop-Location }
    } else {
        $normalized = (Resolve-Path $LocalPath).Path -replace '\\', '/'
        if ($normalized -match '^([A-Za-z]):(.*)$') { $normalized = "/mnt/$($matches[1].ToLower())$($matches[2])" }
        & wsl scp $normalized "aiserver:$RemotePath"
        if ($LASTEXITCODE -ne 0) { throw "scp failed for $leaf (exit $LASTEXITCODE)" }
    }
}

$remotePdf  = "/tmp/$pdfLeaf"
Copy-ToAiserver $resolvedPdf $remotePdf
$extra = ''
$remoteText = ''; $remoteHtml = ''
if ($BodyTextFile -and (Test-Path -LiteralPath $BodyTextFile)) {
    $remoteText = "/tmp/$stem-$Date-body.txt";  Copy-ToAiserver (Resolve-Path $BodyTextFile).Path $remoteText
    $extra += " --body-text-file '$remoteText'"
}
if ($BodyHtmlFile -and (Test-Path -LiteralPath $BodyHtmlFile)) {
    $remoteHtml = "/tmp/$stem-$Date-body.html"; Copy-ToAiserver (Resolve-Path $BodyHtmlFile).Path $remoteHtml
    $extra += " --body-html-file '$remoteHtml'"
}
if ($Subject)   { $safe = $Subject -replace "'", "'\''"; $extra += " --subject '$safe'" }
if ($Recipient) { $safe = $Recipient -replace "'", "'\''"; $extra += " --recipient '$safe'" }
if ($AlsoRecipient) { $safe = $AlsoRecipient -replace "'", "'\''"; $extra += " --also-recipient '$safe'" }

$script = @"
set -euo pipefail
python3 /home/agent/briefing-pipeline/deliver_core.py \
    --pdf '$remotePdf' \
    --runner claude \
    --edition '$Edition' \
    --date '$Date'$extra
# Best effort: /tmp is sticky, so a leftover file owned by another user must not turn a sent briefing into a failure.
rm -f '$remotePdf' '$remoteText' '$remoteHtml' || true
"@
Write-Host 'Delivering via aiserver'
& $invokeRemoteBash -Target aiserver -RunAsUser agent -Script $script
if ($LASTEXITCODE -ne 0) {
    @"
import sys
sys.path.insert(0, r"$scriptsDir")
import run_reporting
run_reporting.update_run_report(r"$Date", run_status="failed", delivery_status="failed")
"@ | py -3 -
    throw "Remote delivery failed with exit code $LASTEXITCODE"
}
# The remote script runs under `set -euo pipefail`, so exit 0 means deliver_core printed "Email sent" (or found its receipt). Print the
# marker here instead of depending on Invoke-RemoteBash to relay the remote stdout; the conductor looks for this exact string.
Write-Host 'Email sent' 
@"
import sys
sys.path.insert(0, r"$scriptsDir")
import run_reporting
run_reporting.update_run_report(r"$Date", delivery_status="sent")   # the conductor owns run_status (completed vs degraded)
"@ | py -3 -
