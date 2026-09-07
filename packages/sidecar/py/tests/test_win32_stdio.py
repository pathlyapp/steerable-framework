"""Windows stdio fallback: threaded stdin pump + synchronous stdout writer,
plus the spill-dir fallback for confined processes without a usable temp dir.

``_connect_stdio`` delegates to ``_connect_stdio_threaded`` on win32 because
ProactorEventLoop cannot register the plain pipe/console handles a child
process inherits from Node/Electron (OSError WinError 6). These tests pin
the fallback's contract on every platform (it is platform-independent code,
only the dispatch is win32-gated).
"""

from __future__ import annotations

import asyncio
import io
import os

import pytest

from steerable_sidecar.sidecar import (
    _ThreadedStdoutWriter,
    _connect_stdio_threaded,
    _spill_directory,
)


class _FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _fake_fd0(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """Route os.read(0) to an in-memory buffer; every other fd delegates."""
    source = io.BytesIO(data)
    real_read = os.read

    def fake_read(fd: int, n: int) -> bytes:
        if fd == 0:
            return source.read(n)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", fake_read)


async def test_threaded_pump_feeds_lines_and_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_fd0(monkeypatch, b'{"a":1}\n{"b":2}\n')
    reader, _writer = _connect_stdio_threaded()

    assert await asyncio.wait_for(reader.readline(), 5) == b'{"a":1}\n'
    assert await asyncio.wait_for(reader.readline(), 5) == b'{"b":2}\n'
    # stdin exhausted -> EOF surfaces so the serve loop shuts down.
    assert await asyncio.wait_for(reader.readline(), 5) == b""


async def test_threaded_pump_immediate_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_fd0(monkeypatch, b"")
    reader, _writer = _connect_stdio_threaded()
    assert await asyncio.wait_for(reader.readline(), 5) == b""


async def test_writer_writes_and_flushes_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStdout()
    monkeypatch.setattr("sys.stdout", fake)
    writer = _ThreadedStdoutWriter()

    assert writer.write(b'{"ok":true}\n') == 12
    await writer.drain()  # no-op, must not raise
    assert fake.buffer.getvalue() == b'{"ok":true}\n'
    assert writer.is_closing() is False


async def test_writer_close_blocks_further_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStdout()
    monkeypatch.setattr("sys.stdout", fake)
    writer = _ThreadedStdoutWriter()

    writer.close()
    assert writer.is_closing() is True
    with pytest.raises(RuntimeError, match="closed"):
        writer.write(b"x")


def test_spill_directory_prefers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEERABLE_SPILL_DIR", "/custom/spill")
    assert _spill_directory() == "/custom/spill"


def test_spill_directory_falls_back_when_temp_unusable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: os.PathLike[str]
) -> None:
    """Confined sidecar (restricted token) has no writable system temp:
    gettempdir() raises FileNotFoundError; the spill dir must fall back to
    the always-writable ~/.steerable root."""
    import tempfile

    monkeypatch.delenv("STEERABLE_SPILL_DIR", raising=False)

    def _no_temp() -> str:
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(tempfile, "gettempdir", _no_temp)
    # Path.home(): POSIX reads HOME, Windows reads USERPROFILE.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _spill_directory() == os.path.join(
        str(tmp_path), ".steerable", "steerable-spill"
    )
