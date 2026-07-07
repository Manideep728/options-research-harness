r"""Launch the dashboard API and frontend together.

Run with the project virtual environment:

    .\.venv\Scripts\python run_dashboard.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"


def _spawn(args: list[str], cwd: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=None,
        stderr=None,
    )


def main() -> int:
    backend = _spawn([sys.executable, str(ROOT / "dashboard_api.py")], ROOT)
    frontend = _spawn(["npm.cmd", "run", "dev"], WEB_DIR)

    print("Dashboard is starting:")
    print("- API: http://127.0.0.1:8000")
    print("- Web: http://localhost:3000")
    print("Press Ctrl+C to stop both processes.")

    processes = [backend, frontend]
    try:
      while True:
        exit_codes = [process.poll() for process in processes]
        if any(code is not None for code in exit_codes):
            break
        time.sleep(0.5)
    except KeyboardInterrupt:
        print("Stopping dashboard...")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())