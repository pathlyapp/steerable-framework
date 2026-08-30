"""Loader for the canonical generic-coding-tool contract (WS4).

The same four generic coding capabilities surface in two products:

* the framework's own ``workspace_tools`` (``read_file`` / ``write_file`` /
  ``edit_file`` / ``bash``) — the headless / ACP / Harbor eval surface;
* the desktop's ``local_*`` tools (``local_read_file`` / ``local_write_file``
  / ``local_edit_file`` / ``local_exec_shell``) — the Electron product, defined
  in ``tool-router.ts`` and executed host-side over the reverse channel.

Both products must agree on the *shared semantic core* so the framework's
evals reflect the desktop's behaviour and a fix on one side propagates to the
other. The single source of truth is the JSON document
``tool_contract.json`` (next to this module). It pins:

* the **version-token protocol** (read-before-write optimistic concurrency):
  a read result carries a ``version`` token; write/edit accept an
  ``expectedVersion`` and reject on mismatch, writing nothing;
* the **version algorithm** (``sha256-utf8-hex``) via hardcoded test vectors,
  so the two independent implementations (Python ``content_version`` and the
  desktop's ``hashContent``) cannot silently diverge;
* the **edit-op semantics** (the algorithm itself is already single-sourced in
  ``file_edit.apply_edits`` and reached by the desktop over RPC);
* the **required input / result fields** per tool.

Product-specific extensions are out of scope and allowed to differ: the
desktop's ``local_`` prefix, its extra ``cwd`` / ``timeout`` / ``createDirs``
fields, and its flat (non ``data``-wrapped) result envelope.

The desktop vendors a copy of this JSON (``contracts/tool-contract.json``) and
asserts its own schemas against it; a co-located sync check fails when the
vendored copy drifts from this canonical source. Bump ``version`` in the JSON
on any semantic change, then re-vendor to the desktop.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("tool_contract.json")


@lru_cache(maxsize=1)
def canonical_contract() -> dict[str, Any]:
    """Return the canonical tool contract parsed from ``tool_contract.json``."""
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def canonical_contract_json() -> str:
    """The canonical contract as its exact on-disk bytes — what the desktop
    vendors and what the co-located sync check compares against."""
    return _CONTRACT_PATH.read_text(encoding="utf-8")
