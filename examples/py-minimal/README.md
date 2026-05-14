# Python Minimal Example

Small example showing protocol + harness usage together.

## Install

```bash
uv add steerable-agent-protocol steerable-agent-harness
```

## Example

```python
from steerable_agent_protocol import ToolCall, ToolResult, SSEEvent
from steerable_agent_harness import BudgetLimit, BudgetState, consume_budget, decide_tool_mode
from steerable_agent_harness.completion import is_terminal_result

call = ToolCall(
    id="call_1",
    name="read_file",
    arguments={"path": "README.md"},
)

mode = decide_tool_mode(call.name)  # "read"

state, exhausted = consume_budget(
    BudgetState(),
    BudgetLimit(max_tokens=5000, max_steps=30, max_tool_calls=10),
    tokens=120,
    step=True,
    tool_call=True,
)

result = ToolResult(
    success=True,
    message=f"mode={mode}, exhausted={exhausted}",
    data={"tokens_used": state.tokens_used},
)

done = is_terminal_result(result.model_dump())

event = SSEEvent(
    type="done" if done else "tool_result",
    payload={"callId": call.id, "result": result.model_dump()},
)
```

Use this as a minimal baseline before integrating your own agent loop.
