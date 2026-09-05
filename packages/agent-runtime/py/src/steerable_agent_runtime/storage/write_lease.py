"""Cross-process write lease for one sqlite database file.

``SqliteStorage`` is asyncio-safe, not process-safe, without this lease.
The desktop sidecar and a second CLI on the same ``--storage-path`` would
otherwise both write WAL. DSH's session JSONL lease is the model: the
kernel is the arbiter, contention fails loud, process death releases the
lock, and there is no TTL that could steal from a live but wedged writer.

POSIX takes a non-blocking ``fcntl.flock`` on a sibling ``*.lock`` file
(``sessions.db`` → ``sessions.lock``). Windows holds a named mutex derived
from the absolute path. The lock file is never deleted: it keeps a stable
inode for later lockers (an unlinked-and-recreated file would be a
different inode). Readers and offline maintenance open the database itself
and do not take this lease; WAL lets them proceed while a writer holds it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

from ..errors import StoreAlreadyOwnedError

_LOCK_ATTEMPTS = 3


def lock_path_for_db(db_path: str | os.PathLike[str]) -> Path:
    """Sibling lock file: ``sessions.db`` → ``sessions.lock``."""
    return Path(db_path).expanduser().resolve().with_suffix(".lock")


def acquire_write_lease(db_path: str | os.PathLike[str]) -> "WriteLease":
    """Acquire the process write lease for ``db_path``.

    ``:memory:`` databases have no file and take no lease.
    """
    raw = os.fspath(db_path)
    if raw == ":memory:" or raw.startswith("file::memory:"):
        return WriteLease._unheld()
    lock_path = lock_path_for_db(raw)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        return WriteLease._acquire_win32(lock_path)
    return WriteLease._acquire_posix(lock_path)


class WriteLease:
    """Held kernel lock. ``release`` closes the descriptor/handle; the file stays."""

    def __init__(self, *, _fd: int | None, _handle: Any, _lock_path: Path | None) -> None:
        self._fd = _fd
        self._handle = _handle
        self._lock_path = _lock_path
        self._released = False

    @classmethod
    def _unheld(cls) -> "WriteLease":
        return cls(_fd=None, _handle=None, _lock_path=None)

    @classmethod
    def _acquire_posix(cls, lock_path: Path) -> "WriteLease":
        import fcntl

        for _ in range(_LOCK_ATTEMPTS):
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise StoreAlreadyOwnedError(str(lock_path)) from exc
                raise
            held = os.fstat(fd)
            try:
                current = os.stat(lock_path)
            except FileNotFoundError:
                os.close(fd)
                continue
            if (held.st_ino, held.st_dev) == (current.st_ino, current.st_dev):
                return cls(_fd=fd, _handle=None, _lock_path=lock_path)
            os.close(fd)
        raise StoreAlreadyOwnedError(str(lock_path))

    @classmethod
    def _acquire_win32(cls, lock_path: Path) -> "WriteLease":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        digest = hashlib.sha256(str(lock_path).encode("utf-8")).hexdigest()
        name = f"Local\\steerable-store-{digest}"
        error_already_exists = 183
        handle = create_mutex(None, True, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == error_already_exists:
            close_handle(handle)
            raise StoreAlreadyOwnedError(str(lock_path))
        return cls(_fd=None, _handle=handle, _lock_path=lock_path)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            close_handle(self._handle)
            self._handle = None

    def __enter__(self) -> "WriteLease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
