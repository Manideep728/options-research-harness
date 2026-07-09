"""Single-instance lock for the trading engine, backed by a PID file.

Why this exists: the dashboard API used to track "is the engine running?"
purely in an in-memory subprocess.Popen handle. That handle dies whenever the
API process restarts (crash, reload, manual bounce) while the engine keeps
running — so the dashboard would report "stopped" and a click on Start would
spawn a second engine racing the first one over the same broker account.

A PID file survives an API restart: any process (the API, a second `python
main.py`, a human) can check "is the process that owns this PID file still
alive?" without needing to have started it."""

import os
from pathlib import Path


def acquire(path: str) -> bool:
    """Claim the lock for the current process. Returns False if another live
    process already holds it (caller should refuse to start)."""
    existing = read_pid(path)
    if existing is not None and _pid_alive(existing):
        return False
    Path(path).write_text(str(os.getpid()))
    return True


def release(path: str) -> None:
    file = Path(path)
    try:
        if read_pid(path) == os.getpid():
            file.unlink()
    except OSError:
        pass


def read_pid(path: str) -> int | None:
    file = Path(path)
    if not file.exists():
        return None
    try:
        return int(file.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(path: str) -> bool:
    pid = read_pid(path)
    return pid is not None and _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
