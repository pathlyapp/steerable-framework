"""Test-only sidecar: the real chat path with a progressive default spec.

Run as a subprocess by ``test_e2e_tool_exposure.py``. Boots the production
``Sidecar.serve()`` with the bundled-spec loader replaced by a spec whose
tools dimension is ``progressive`` — the same monkeypatch seam the
in-process unit tests use, applied inside a real process so the unwired
selection failure is observed on the wire (``stream.error``), not in a
mocked call stack.

The chat path has no in-process ToolRouter for host-supplied tools, so
``ProgressiveDisclosure.select`` must raise rather than offer a
``tool_search`` descriptor nothing can dispatch.
"""

from __future__ import annotations

import asyncio
import sys

import steerable_sidecar.sidecar as sidecar_mod
from steerable_agent_runtime.harness_spec import harness_spec_from_dict
from steerable_sidecar import Sidecar, SidecarConfig

_SPEC = {
    "context": ["null"],
    "retry": ["none"],
    "validator": "null",
    "tools": "progressive",
    "memory": "stateless",
    "orchestration": "single",
}


def _progressive_spec():
    return harness_spec_from_dict(_SPEC)


# Replace the bundled-spec loader before serve(); the chat path resolves
# the spec through this module global on every turn.
sidecar_mod._default_harness_spec = _progressive_spec


if __name__ == "__main__":
    try:
        asyncio.run(Sidecar(config=SidecarConfig(log_level="ERROR")).serve())
    except KeyboardInterrupt:
        pass
    except Exception:  # boot failures must be visible on stderr, not silent
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
