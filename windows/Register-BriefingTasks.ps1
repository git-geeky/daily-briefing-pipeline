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

    Defaults per edition (override with the parameters; keep the watchdog after the conductor's
    deadline_local in editions/<edition>.json):
        animal  06:30 start, repeats until 11:30, watchdog 11:45   (deadline 11:30)
        daily   12:30 start, repeats until 17:30, watchdog 17:45   (deadline 17:30)
    The two editions are staggered so they do not share one five-hour usage window.

    Even when enabled, the first run is TOMORROW's occurrence: the trigger's start boundary is set to tomorrow, so
    nothing fires today. Pass -AllowToday only when you really want today's occurrence (it is never the default).

    -NoDeliver registers the tasks in burn-in mode: every scheduled run builds the briefing (PDF, combined PDF) and sends nothing, so the first
    days can be read before anyone receives anything. Re-register without it (same command, -Force semantics) to start sending. The run that
    finds today's briefing already built exits at once, so the repetitions do not rebuild it.

    Tasks are created DISABLED unless -Enable is passed. The legacy \ClaudeCode\Claude-DailyBriefing
    task is left alone (it is disabled); do not enable both.

.EXAMPLE
    .\Register-BriefingTasks.ps1 -Edition daily -Enable -NoDeliver     # burn-in: builds, sends nothing
    .\Register-BriefingTasks.ps1 -Edition animal -WhatIf
    .\Register-BriefingTasks.ps1 -Edition animal -Enable

.NOTES
    UNTESTED on the target machine (written from the cloud). Times are the machine's local time.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('animal', 'daily')][string]$Edition = 'animal',
    [string]$StartAt = '',
    [string]$WatchdogAt = '',
    [int]$RepeatMinutes = 30,
    [int]$RepeatHours = 0,
    [switch]$Enable,
    [switch]$AllowToday,
    [switch]$NoDeliver
)

$ErrorActionPreference = 'Stop'
$defaults = @{
    animal = @{ Start = '06:30'; Watchdog = '11:45'; Hours = 5 }
    daily  = @{ Start = '12:30'; Watchdog = '17:45'; Hours = 5 }
}
if (-not $StartAt)    { $StartAt    = $defaults[$Edition].Start }
if (-not $WatchdogAt) { $WatchdogAt = $defaults[$Edition].Watchdog }
if ($RepeatHours -le 0) { $RepeatHours = $defaults[$Edition].Hours }

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $here 'Run-Briefing.ps1'
$vbs = Join-Path $env:USERPROFILE 'bin\run-ps-hidden.vbs'   # same hidden-window shim the existing tasks use
if (-not (Test-Path -LiteralPath $vbs)) { throw "Hidden launcher shim not found: $vbs" }

function New-BriefingTask([string]$Name, [string]$Arguments, [string]$At, [int]$LimitHours, [switch]$Repeat) {
    if ($NoDeliver) { $Arguments = "$Arguments -NoDeliver" }
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`" `"$launcher`" $Arguments"
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    if (-not $AllowToday) {
        # Daily triggers start at the next occurrence of $At, which may be later today. Pin the first one to tomorrow.
        $time = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
        $trigger.StartBoundary = ([datetime]::Today.AddDays(1).Date + $time.TimeOfDay).ToString('s')
    }
    if ($Repeat) {
        $rep = (New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Hours $RepeatHours)).Repetition
        $trigger.Repetition = $rep
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours $LimitHours) -MultipleInstances IgnoreNew
    if (-not $Enable) { $settings.Enabled = $false }
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    $first = if ($AllowToday) { 'next occurrence (possibly today)' } else { "tomorrow ($([datetime]::Today.AddDays(1).ToString('yyyy-MM-dd')))" }
    if ($PSCmdlet.ShouldProcess("\Briefing\$Name", "Register ($Arguments at $At; first run $first)")) {
        Register-ScheduledTask -TaskName $Name -TaskPath '\Briefing\' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
        Write-Host "Registered \Briefing\$Name (enabled: $([bool]$Enable); first run: $first; delivery: $(if ($NoDeliver) { 'OFF (burn-in, builds only)' } else { 'on' }))"
    }
}

New-BriefingTask -Name "Briefing-$Edition" -Arguments "-Edition $Edition -Scheduled" -At $StartAt -LimitHours 4 -Repeat
New-BriefingTask -Name "Briefing-$Edition-Watchdog" -Arguments "-Edition $Edition -Finish -Scheduled" -At $WatchdogAt -LimitHours 1
if ($Edition -eq 'animal') {
    # The animal edition rides inside the daily briefing. If the daily briefing failed or never ran, the held animal edition would be
    # lost; this release task re-checks after the daily deadline and sends it on its own (late) when the daily briefing did not carry it.
    New-BriefingTask -Name "Briefing-animal-Release" -Arguments "-Edition animal -Finish -Scheduled" -At '18:00' -LimitHours 1
}
