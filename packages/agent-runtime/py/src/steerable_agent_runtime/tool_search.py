"""Discovery seam for the deferred tool tier.

Deferred tools are registered (dispatchable by name) but omitted from the
model-visible list, so the offered list stays bounded as registrations
grow. ``tool_search`` is how the model finds them: one direct-tier tool
that BM25-ranks the deferred inventory over name + description and returns
full descriptors. A match is immediately callable — dispatch never gated on
exposure — so discovery costs one tool round instead of a permanent list
slot.

Opt-in, same shape as the subagent seam: the host calls
`register_tool_search` and appends `tool_search_descriptor` to the loop's
tools list; routers without a deferred tier simply do neither. The
``progressive`` harness strategy wires both halves from
``AssembledHarness.wire_tools`` (see `harness_spec`).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from .tools import RegisteredTool, ToolRouter

TOOL_SEARCH_NAME = "tool_search"

#: Default matches per call. 8 matches codex's tool_discovery cap
#: (docs/roadmap.md's tier comparison); the per-call ceiling below bounds
#: the schema payload regardless of what a call requests.
DEFAULT_MAX_RESULTS = 8
#: Hard ceiling per call: every match carries a full schema, so an
#: unbounded result set is an unbounded context injection.
MAX_RESULTS_CEILING = 20

#: BM25 saturation/length-normalization constants — the Lucene defaults
#: (Robertson & Zaragoza 2009), unchanged: the inventory is small (MCP
#: catalogs cap at 64 tools/server), so retuning buys nothing.
_BM25_K1 = 1.2
_BM25_B = 0.75
#: Name tokens index twice: a hit in the tool's own name signals intent
#: more than one in prose (the pre-BM25 scorer's 2:1 weighting, carried).
_NAME_WEIGHT = 2


def tool_search_descriptor(
    *, name: str = TOOL_SEARCH_NAME, max_results: int = DEFAULT_MAX_RESULTS
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
                            "Search terms ranked against deferred tool names "
                            "and descriptions, best matches first."
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


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric runs: ``mcp__github__create_issue`` tokenizes
    to its qualifier and verb parts, which is how a model phrases queries."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _rank(
    inventory: list[RegisteredTool], query: str
) -> list[tuple[float, RegisteredTool]]:
    """BM25-rank ``inventory`` against ``query``, best first.

    Ranks rather than filters, but the floor stays a filter: a document
    containing no query term scores 0 and is dropped — an empty result for
    an off-vocabulary query beats a list of irrelevant tools.
    """
    documents = [
        (tool, _tokenize(tool.name) * _NAME_WEIGHT + _tokenize(tool.description))
        for tool in inventory
    ]
    if not documents:
        return []
    doc_freq = Counter(
        term for _, tokens in documents for term in set(tokens)
    )
    n_docs = len(documents)
    avg_len = sum(len(tokens) for _, tokens in documents) / n_docs
    scored: list[tuple[float, RegisteredTool]] = []
    for tool, tokens in documents:
        term_freq = Counter(tokens)
        score = 0.0
        for term in set(_tokenize(query)):
            freq = term_freq.get(term, 0)
            if freq == 0:
                continue
            n = doc_freq[term]
            idf = math.log(1 + (n_docs - n + 0.5) / (n + 0.5))
            score += idf * (freq * (_BM25_K1 + 1)) / (
                freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(tokens) / avg_len)
            )
        if score > 0:
            scored.append((score, tool))
    # Deterministic order: score desc, then name.
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return scored


def register_tool_search(
    router: ToolRouter,
    *,
    name: str = TOOL_SEARCH_NAME,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> RegisteredTool:
    """Register the discovery tool on ``router`` (direct tier).

    The handler BM25-ranks the router's deferred registrations at call
    time, so tools deferred after registration are still found. Hidden
    tools are never searchable. Results are bounded (``max_results``,
    overridable per call up to ``MAX_RESULTS_CEILING``) and carry the full
    schema so the model can call a match without a second round-trip.
    """

    def _search(query: str, max_results: int = max_results) -> dict[str, Any]:
        ranked = _rank(router.deferred_tools(), query)
        cap = max(1, min(int(max_results), MAX_RESULTS_CEILING))
        matches = [
            {
                "name": registered.name,
                "description": registered.description,
                "parameters": registered.schema,
            }
            for _, registered in ranked[:cap]
        ]
        return {
            "matches": matches,
            "deferredCount": len(router.deferred_tools()),
            "note": (
                "Matched tools are registered; call them by name with the "
                "returned schema."
                if matches
                else "No deferred tools matched; try different search terms."
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
