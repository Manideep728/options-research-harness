"""Atomic file writes: write to a temp file in the same directory, then
os.replace() over the target. A reader never observes a partially-written
file, and a crash mid-write leaves the old file intact rather than a
truncated one (plain Path.write_text() truncates first)."""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
