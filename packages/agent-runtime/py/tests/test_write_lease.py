"""Process write lease: one writer per sqlite path; death releases; no TTL steal."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from steerable_agent_runtime.errors import StoreAlreadyOwnedError
from steerable_agent_runtime.storage import SqliteStorage
from steerable_agent_runtime.storage.write_lease import (
    acquire_write_lease,
    lock_path_for_db,
)

_HOLDER = r"""
import sys, time
from pathlib import Path
from steerable_agent_runtime.storage.write_lease import acquire_write_lease
lease = acquire_write_lease(sys.argv[1])
Path(sys.argv[2]).write_text("held", encoding="utf-8")
time.sleep(60)
"""


def _spawn_holder(db_path: Path, flag: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(db_path), str(flag)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_held(proc: subprocess.Popen[str], flag: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"holder exited {proc.returncode} before taking the lease: {err}"
            )
        if flag.exists() and flag.read_text(encoding="utf-8").strip() == "held":
            return
        time.sleep(0.05)
    err = proc.stderr.read() if proc.stderr else ""
    raise AssertionError(f"holder did not take the lease: {err}")


def test_lock_path_is_sibling_lock_file(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    assert lock_path_for_db(db) == tmp_path.resolve() / "sessions.lock"


def test_second_process_fails_loud(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    flag = tmp_path / "held"
    holder = _spawn_holder(db, flag)
    try:
        _wait_held(holder, flag)
        with pytest.raises(StoreAlreadyOwnedError, match="already owned"):
            acquire_write_lease(db)
        with pytest.raises(StoreAlreadyOwnedError):
            SqliteStorage(str(db))
        assert lock_path_for_db(db).is_file()
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_successor_opens_after_holder_is_killed(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    flag = tmp_path / "held"
    holder = _spawn_holder(db, flag)
    try:
        _wait_held(holder, flag)
        holder.kill()
        holder.wait(timeout=5)
    except BaseException:
        holder.kill()
        holder.wait(timeout=5)
        raise
    store = SqliteStorage(str(db))
    store.close()
    assert lock_path_for_db(db).is_file()


def test_release_does_not_delete_lock_file(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    lease = acquire_write_lease(db)
    lock = lock_path_for_db(db)
    assert lock.is_file()
    lease.release()
    assert lock.is_file()
    again = acquire_write_lease(db)
    again.release()
    assert lock.is_file()
