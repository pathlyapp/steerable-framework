"""ACP fs client bridge (3.4.3.1): workspace file content via the editor.

Inside an editor embedding, disk is not the source of truth — the
editor's unsaved buffers are. When the client advertises
``fs.readTextFile`` / ``fs.writeTextFile``, the adapter builds the
session's workspace tools with this channel so ``read_file`` /
``write_file`` / ``edit_file`` / ``apply_patch`` all flow through
``fs/read_text_file`` / ``fs/write_text_file`` round trips.

Each direction falls back to local disk when the client did not advertise
it. A bridged read with a local write can leave the editor's buffer
diverging from disk, but surfacing that conflict is the editor's job (it
owns the buffer); the pre-bridge behavior wrote to disk unconditionally,
so the fallback is no regression.

The version-token conflict check (``expectedVersion``) is channel-
agnostic: it re-reads through this same channel before writing, so a
stale-token rejection works identically over the bridge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workspace_fs import LOCAL_FS, WorkspaceFsError


class AcpWorkspaceFs:
    """``WorkspaceFs`` over the ACP client's fs methods."""

    def __init__(
        self,
        conn: Any,
        session_id: str,
        *,
        bridge_read: bool = True,
        bridge_write: bool = True,
    ) -> None:
        self._conn = conn
        self._session_id = session_id
        self._bridge_read = bridge_read
        self._bridge_write = bridge_write

    async def read_text(self, target: Path) -> str:
        if not self._bridge_read:
            return await LOCAL_FS.read_text(target)
        try:
            response = await self._conn.read_text_file(
                self._session_id, str(target)
            )
        except Exception as exc:  # client transport/refusal — fail loud
            raise WorkspaceFsError(f"ACP fs/read_text_file failed: {exc}") from exc
        return str(response.content)

    async def write_text(self, target: Path, content: str) -> None:
        if not self._bridge_write:
            return await LOCAL_FS.write_text(target, content)
        try:
            await self._conn.write_text_file(
                self._session_id, str(target), content
            )
        except Exception as exc:  # client transport/refusal — fail loud
            raise WorkspaceFsError(f"ACP fs/write_text_file failed: {exc}") from exc
