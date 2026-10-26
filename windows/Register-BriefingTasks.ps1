<#
.SYNOPSIS
    Register the briefing scheduled tasks (conductor + watchdog) under \Briefing\.

.DESCRIPTION
    Per edition two tasks are created:
      * Briefing-<Edition>          daily at -StartAt, repeating every -RepeatMinutes for -RepeatHours.
                                    The conductor is idempotent: once delivered it exits at once, and
                                    after a usage-limit deferral the next repetition resumes the run.
      * Briefing-<Edition>-Watchdog daily at -WatchdogAt: assembles and delivers whatever exists, or
                                    raises an alert, so a missing briefing can never be silent.
    Tasks are created DISABLED unless -Enable is passed. The legacy \ClaudeCode\Claude-DailyBriefing
    task is left alone (it is disabled); do not enable both.

.EXAMPLE
    .\Register-BriefingTasks.ps1 -Edition animal -WhatIf
    .\Register-BriefingTasks.ps1 -Edition animal -Enable

.NOTES
    UNTESTED on the target machine (written from the cloud). Times are the machine's local time.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('animal', 'daily')][string]$Edition = 'animal',
    [string]$StartAt = '06:30',
    [string]$WatchdogAt = '11:45',
    [int]$RepeatMinutes = 30,
    [int]$RepeatHours = 5,
    [switch]$Enable
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $here 'Run-Briefing.ps1'
$vbs = Join-Path $env:USERPROFILE 'bin\run-ps-hidden.vbs'   # same hidden-window shim the existing tasks use
if (-not (Test-Path -LiteralPath $vbs)) { throw "Hidden launcher shim not found: $vbs" }

function New-BriefingTask([string]$Name, [string]$Arguments, [string]$At, [switch]$Repeat) {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`" `"$launcher`" $Arguments"
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    if ($Repeat) {
        $rep = (New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Hours $RepeatHours)).Repetition
        $trigger.Repetition = $rep
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4) -MultipleInstancesPolicy IgnoreNew
    if (-not $Enable) { $settings.Enabled = $false }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    if ($PSCmdlet.ShouldProcess("\Briefing\$Name", "Register ($Arguments at $At)")) {
        Register-ScheduledTask -TaskName $Name -TaskPath '\Briefing\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "Registered \Briefing\$Name (enabled: $([bool]$Enable))"
    }
}

New-BriefingTask -Name "Briefing-$Edition" -Arguments "-Edition $Edition" -At $StartAt -Repeat
New-BriefingTask -Name "Briefing-$Edition-Watchdog" -Arguments "-Edition $Edition -Finish" -At $WatchdogAt
