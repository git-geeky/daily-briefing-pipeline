<#
.SYNOPSIS
    Email an operational alert (briefing failed / deferred past deadline / action needed) via aiserver.
    The conductor always writes the alert to <generated>\alerts first; this is the optional email leg.
.NOTES
    UNTESTED on the target machine (written from the cloud).
#>
param(
    [Parameter(Mandatory = $true)][string]$Subject,
    [Parameter(Mandatory = $true)][string]$BodyFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'   # native stderr must not abort the script; native calls check $LASTEXITCODE
if (-not (Test-Path -LiteralPath $BodyFile)) { throw "Alert body not found: $BodyFile" }
if (-not (Get-Command Invoke-RemoteBash -ErrorAction SilentlyContinue)) {
    $toolsPath = Join-Path $HOME 'bin\codex-ssh-tools.ps1'
    if (Test-Path -LiteralPath $toolsPath) { . $toolsPath }
}
$invokeRemoteBash = Get-Command Invoke-RemoteBash -ErrorAction Stop
$transport = Get-Command Get-RemoteSshTransport -ErrorAction Stop | ForEach-Object { & $_ }

$resolved = (Resolve-Path $BodyFile).Path
$remote = "/tmp/briefing-alert-$([guid]::NewGuid().ToString('N')).txt"
if ($transport -eq 'windows') {
    Push-Location (Split-Path $resolved -Parent)
    try { & scp ".\$(Split-Path $resolved -Leaf)" "aiserver:$remote"; if ($LASTEXITCODE -ne 0) { throw "scp failed" } } finally { Pop-Location }
} else {
    $normalized = $resolved -replace '\\', '/'
    if ($normalized -match '^([A-Za-z]):(.*)$') { $normalized = "/mnt/$($matches[1].ToLower())$($matches[2])" }
    & wsl scp $normalized "aiserver:$remote"; if ($LASTEXITCODE -ne 0) { throw "scp failed" }
}
$safeSubject = $Subject -replace "'", "'\''"
$script = @"
set -euo pipefail
python3 /home/agent/briefing-pipeline/deliver_core.py --runner diff --body-from-stdin --subject '$safeSubject' < '$remote'
rm -f '$remote' || true
"@
& $invokeRemoteBash -Target aiserver -RunAsUser agent -Script $script
if ($LASTEXITCODE -ne 0) { throw "Alert delivery failed with exit code $LASTEXITCODE" }
