"""acquire_instance_lock: only one instance may hold the lock at a time."""

import fcntl
import os

import pytest

from app import acquire_instance_lock


@pytest.fixture
def lock_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_second_instance_is_refused(lock_dir):
    first = acquire_instance_lock()
    assert first is not None

    assert acquire_instance_lock() is None

    os.close(first)


def test_lock_is_released_when_process_exits(lock_dir):
    first = acquire_instance_lock()
    os.close(first)

    again = acquire_instance_lock()
    assert again is not None
    os.close(again)


def test_lock_is_exclusive_against_other_processes(lock_dir):
    fd = acquire_instance_lock()
    assert fd is not None

    path = lock_dir / "remote-apt-dater.lock"
    other = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with pytest.raises(OSError):
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    os.close(other)
    os.close(fd)
