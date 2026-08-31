"""File-content channel behind the workspace file tools (3.4.3.1).

The workspace tools (read/write/edit/apply_patch) never touch disk
directly; they go through a ``WorkspaceFs``. The local default reads and
writes disk exactly as the tools always have. The ACP editor bridge
(``acp_fs.AcpWorkspaceFs``) routes the same operations through the client,
so an agent embedded in an editor reads the editor's unsaved buffers
instead of stale disk contents and writes back through the editor's own
conflict surface.

Both directions are async so a client round trip fits the contract.
Implementations raise ``WorkspaceFsError`` for transport-level failures;
``LocalFs`` raises plain ``OSError`` — tool wrappers catch both.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Protocol

_tmp_counter = itertools.count()


class WorkspaceFsError(Exception):
    """A non-local workspace fs channel failed (transport, client refusal)."""


class WorkspaceFs(Protocol):
    """Read/write file contents under an already-resolved workspace path.

    Path jurisdiction (staying under the workspace root) is the caller's
    job — implementations receive an absolute, in-workspace ``target``.
    ``write_text`` creates parent directories as needed.
    """

    async def read_text(self, target: Path) -> str:
        """Return the full UTF-8 text of ``target``."""
        ...

    async def write_text(self, target: Path, content: str) -> None:
        """Replace ``target``'s contents with ``content``."""
        ...


class LocalFs:
    """Disk-backed channel: the headless/Harbor behavior."""

    async def read_text(self, target: Path) -> str:
        return target.read_text(encoding="utf-8")

    async def write_text(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write never leaves a truncated file.
        tmp = target.with_name(
            f"{target.name}.tmp-{os.getpid()}-{next(_tmp_counter)}"
        )
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


#: Stateless, so one shared instance is enough.
LOCAL_FS = LocalFs()
