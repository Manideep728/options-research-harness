# Registers (or re-registers) the Windows Task Scheduler jobs that run the
# bot automatically Mon-Fri during the trading window. Run once:
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
# Remove with:
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
param([switch]$Unregister)

$StartTime = '08:00'   # keep in sync with start_bot.ps1 defaults
$StopTime = '15:30'
$Days = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'

if ($Unregister) {
    Unregister-ScheduledTask -TaskName 'TradingBot Start' -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'TradingBot Stop' -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'TradingBotStart.cmd') `
        -Force -ErrorAction SilentlyContinue
    Write-Output 'TradingBot tasks and startup entry removed.'
    exit 0
}

$scriptsDir = $PSScriptRoot
$psArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}"'

# StartWhenAvailable: if the PC was off/asleep at 8:00, the task fires as
# soon as it's back; start_bot.ps1's own window check makes that safe.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# NOTE: no -AtLogOn trigger — registering one needs admin rights. Boot/wake
# coverage comes from StartWhenAvailable (a missed 8:00 firing runs as soon
# as the PC is back) plus a Startup-folder entry created below.
$startAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ($psArgs -f (Join-Path $scriptsDir 'start_bot.ps1'))
$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $StartTime
Register-ScheduledTask -TaskName 'TradingBot Start' -Action $startAction `
    -Trigger $startTrigger -Settings $settings -Force | Out-Null

# Startup-folder entry: fires at every logon, window check makes it a no-op
# outside trading hours. No admin needed, easy to delete.
$startupCmd = Join-Path ([Environment]::GetFolderPath('Startup')) 'TradingBotStart.cmd'
"@echo off`r`nstart /min `"`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $scriptsDir 'start_bot.ps1')`"" |
    Set-Content -Path $startupCmd -Encoding ascii

$stopAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ($psArgs -f (Join-Path $scriptsDir 'stop_bot.ps1'))
$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $StopTime
Register-ScheduledTask -TaskName 'TradingBot Stop' -Action $stopAction `
    -Trigger $stopTrigger -Settings $settings -Force | Out-Null

Write-Output "Registered: 'TradingBot Start' (Mon-Fri $StartTime + logon via Startup folder), 'TradingBot Stop' (Mon-Fri $StopTime)."
