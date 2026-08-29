"""Discovery seam for the deferred tool tier.

Deferred tools are registered (dispatchable by name) but omitted from the
model-visible list, so the offered list stays bounded as registrations
grow. ``tool_search`` is how the model finds them: one direct-tier tool
that keyword-matches the deferred inventory and returns full descriptors.
A match is immediately callable — dispatch never gated on exposure — so
discovery costs one tool round instead of a permanent list slot.

Opt-in, same shape as the subagent seam: the host calls
`register_tool_search` and appends `tool_search_descriptor` to the loop's
tools list; routers without a deferred tier simply do neither.
"""

from __future__ import annotations

from typing import Any

from .tools import RegisteredTool, ToolRouter

TOOL_SEARCH_NAME = "tool_search"


def tool_search_descriptor(
    *, name: str = TOOL_SEARCH_NAME, max_results: int = 5
) -> dict[str, Any]:
    """OpenAI tool schema to append to the loop's tools list."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (
                "Search for additional tools not in the initial tool list. "
                "Returns full tool schemas; a matched tool can be called "
                "immediately by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords to match against deferred tool names "
                            "and descriptions."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Cap on returned schemas (default {max_results})."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def register_tool_search(
    router: ToolRouter,
    *,
    name: str = TOOL_SEARCH_NAME,
    max_results: int = 5,
) -> RegisteredTool:
    """Register the discovery tool on ``router`` (direct tier).

    The handler searches the router's deferred registrations at call time,
    so tools deferred after registration are still found. Hidden tools are
    never searchable. Results are bounded (``max_results``, overridable per
    call) and carry the full schema so the model can call a match without
    a second round-trip.
    """

    def _search(query: str, max_results: int = max_results) -> dict[str, Any]:
        terms = query.lower().split()
        scored: list[tuple[int, RegisteredTool]] = []
        for registered in router.deferred_tools():
            haystack_name = registered.name.lower()
            haystack_desc = registered.description.lower()
            # Name hits outweigh description hits; every term must hit
            # somewhere (AND semantics keep result sets small and relevant).
            score = 0
            for term in terms:
                if term in haystack_name:
                    score += 2
                elif term in haystack_desc:
                    score += 1
                else:
                    score = -1
                    break
            if score > 0:
                scored.append((score, registered))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        cap = max(1, min(int(max_results), 20))
        matches = [
            {
                "name": registered.name,
                "description": registered.description,
                "parameters": registered.schema,
            }
            for _, registered in scored[:cap]
        ]
        return {
            "matches": matches,
            "deferredCount": len(router.deferred_tools()),
            "note": (
                "Matched tools are registered; call them by name with the "
                "returned schema."
                if matches
                else "No deferred tools matched; try different keywords."
            ),
        }

    return router.register(
        _search,
        name=name,
        mode="read",
        description=(
            "Search for additional tools not in the initial tool list."
        ),
        schema=tool_search_descriptor(name=name, max_results=max_results)[
            "function"
        ]["parameters"],
    )
