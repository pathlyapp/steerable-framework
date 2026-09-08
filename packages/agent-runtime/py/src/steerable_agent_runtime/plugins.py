"""Third-party tool discovery via ``importlib.metadata`` entry points.

A package registers tools by declaring the ``steerable.tools`` entry-point
group in its own packaging metadata::

    # pyproject.toml of the extension package
    [project.entry-points."steerable.tools"]
    my_tools = "my_package.tools:register"

The referenced object is called with the host's ``ToolRouter`` and registers
its ``@tool`` functions on it — declare-and-register in one step, no host
code change per extension. This is the framework's minimal extension runtime:
deliberately not a full plugin system (no lifecycle, no config schema, no
isolation), just a discovery seam so an installed package can add tools.

Misconfiguration fails loud at load: an entry point that does not import, or
whose object is not callable, raises ``PluginLoadError`` naming the offender
rather than being silently skipped — a tool the user installed and the host
dropped is a silent capability loss.
"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from .tools import ToolRouter

logger = logging.getLogger(__name__)

#: The entry-point group third-party packages register tools under.
STEERABLE_TOOLS_ENTRY_POINT_GROUP = "steerable.tools"


class PluginLoadError(RuntimeError):
    """An entry point in the ``steerable.tools`` group failed to load."""


def load_tool_entry_points(
    router: ToolRouter,
    *,
    group: str = STEERABLE_TOOLS_ENTRY_POINT_GROUP,
) -> list[str]:
    """Discover and load every entry point in ``group`` onto ``router``.

    Each entry point must resolve to a callable taking the router. Returns the
    names of the entry points that loaded, in discovery order. Raises
    ``PluginLoadError`` on the first offender (import failure or non-callable).
    """
    loaded: list[str] = []
    for ep in metadata.entry_points(group=group):
        try:
            target: Any = ep.load()
        except Exception as exc:  # noqa: BLE001 — re-raised with the offender named
            raise PluginLoadError(
                f"entry point {ep.name!r} ({ep.value}) failed to import: {exc}"
            ) from exc
        if not callable(target):
            raise PluginLoadError(
                f"entry point {ep.name!r} ({ep.value}) is not callable: "
                f"got {type(target).__name__}"
            )
        target(router)
        loaded.append(ep.name)
        logger.info("loaded tool entry point %s (%s)", ep.name, ep.value)
    return loaded
