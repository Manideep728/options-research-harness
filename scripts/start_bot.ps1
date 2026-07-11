# Starts the trading engine, but only inside the trading window.
# Called by Task Scheduler at logon and at window-open (see register_tasks.ps1).
# Outside the window (or on weekends, or if already running) it exits silently,
# so it is always safe to run. main.py's own PID lock is the second guard.
param(
    [int]$StartHour = 8,     # window opens 8:00 AM local (pre-market buffer;
                             # CT market hours are 8:30 AM - 3:00 PM)
    [int]$StopHour = 15,     # window closes 3:30 PM local - grace past the
    [int]$StopMinute = 30    # close so a final exit order isn't cut off
)

$Root = Split-Path -Parent $PSScriptRoot

$now = Get-Date
if ($now.DayOfWeek -eq 'Saturday' -or $now.DayOfWeek -eq 'Sunday') { exit 0 }
$windowStart = Get-Date -Hour $StartHour -Minute 0 -Second 0
$windowEnd = Get-Date -Hour $StopHour -Minute $StopMinute -Second 0
if ($now -lt $windowStart -or $now -ge $windowEnd) { exit 0 }

# Already running? (engine.pid is the single-instance lock main.py maintains)
$pidFile = Join-Path $Root 'engine.pid'
if (Test-Path $pidFile) {
    $enginePid = (Get-Content $pidFile -ErrorAction SilentlyContinue) -as [int]
    if ($enginePid -and (Get-Process -Id $enginePid -ErrorAction SilentlyContinue)) {
        exit 0
    }
}

$python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { exit 1 }
Start-Process -FilePath $python -ArgumentList 'main.py' `
    -WorkingDirectory $Root -WindowStyle Hidden
