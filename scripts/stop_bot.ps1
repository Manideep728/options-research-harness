# Stops the trading engine at window close. Safe to run any time: if the
# engine isn't running this is a no-op. A hard kill is fine by design — the
# engine persists day-state on every entry and reconciles orders/fills from
# the broker on the next start, so nothing is lost between sessions.
$Root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $Root 'engine.pid'

if (-not (Test-Path $pidFile)) { exit 0 }
$enginePid = (Get-Content $pidFile -ErrorAction SilentlyContinue) -as [int]
if ($enginePid) {
    $proc = Get-Process -Id $enginePid -ErrorAction SilentlyContinue
    # Only kill if the PID is actually our python engine, not a reused PID
    if ($proc -and $proc.ProcessName -like 'python*') {
        Stop-Process -Id $enginePid -Force
    }
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
