"""End-to-end smoke test of protocol + harness + runtime in a single file.

Run with:
    uv run --package steerable-example-py-minimal python -m steerable_example_py_minimal.main
"""

from __future__ import annotations

import asyncio

from steerable_agent_harness import (
    BudgetLimit,
    BudgetState,
    consume_budget,
    decide_tool_mode,
    is_terminal_result,
)
from steerable_agent_protocol import SSEEvent, ToolCall, ToolResult
from steerable_agent_runtime import ToolRouter, tool


router = ToolRouter()


@tool(router=router, description="Read a file by path")
async def read_file(path: str) -> dict:
    """Pretend-read a file. Real impl would do `open(path).read()`."""
    return {"path": path, "content": f"<contents of {path}>"}


async def main() -> None:
    call = ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})

    # Tier 2 — classify and budget
    mode = decide_tool_mode(call.name)
    state, exhausted = consume_budget(
        BudgetState(),
        BudgetLimit(max_tokens=5_000, max_steps=30, max_tool_calls=10),
        tokens=120,
        step=True,
        tool_call=True,
    )
    print(f"[harness] mode={mode!r}  budget_exhausted={exhausted}  state={state}")

    # Tier 3 — actually run the tool
    result: ToolResult = await router.dispatch(call)
    print(f"[runtime] success={result.success}  data={result.data}")

    # Tier 1 — emit a wire event
    done = is_terminal_result(result.model_dump())
    event = SSEEvent(
        type="done" if done else "tool_result",
        payload={"callId": call.id, "result": result.model_dump()},
    )
    print(f"[wire]    {event.model_dump_json()}")


if __name__ == "__main__":
    asyncio.run(main())
