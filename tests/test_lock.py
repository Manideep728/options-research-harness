import os

from bot import lock


def test_acquire_succeeds_when_no_lock_exists(tmp_path):
    path = str(tmp_path / "engine.pid")
    assert lock.acquire(path) is True
    assert lock.read_pid(path) == os.getpid()


def test_acquire_fails_when_a_live_process_holds_it(tmp_path):
    path = str(tmp_path / "engine.pid")
    # This test process is itself alive, so writing its own PID simulates
    # "another live instance holds the lock".
    with open(path, "w") as f:
        f.write(str(os.getpid()))
    assert lock.acquire(path) is False


def test_acquire_succeeds_over_a_stale_pid(tmp_path):
    path = str(tmp_path / "engine.pid")
    # PID 999999 should not correspond to a live process on any dev machine.
    with open(path, "w") as f:
        f.write("999999")
    assert lock.acquire(path) is True
    assert lock.read_pid(path) == os.getpid()


def test_is_running_reflects_lock_state(tmp_path):
    path = str(tmp_path / "engine.pid")
    assert lock.is_running(path) is False
    lock.acquire(path)
    assert lock.is_running(path) is True


def test_release_only_removes_own_lock(tmp_path):
    path = str(tmp_path / "engine.pid")
    with open(path, "w") as f:
        f.write("999999")  # someone else's (stale) lock
    lock.release(path)
    assert lock.read_pid(path) == 999999  # untouched: we never held it
