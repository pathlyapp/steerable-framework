# Python Minimal Example

End-to-end smoke test that exercises all three Python tiers:

- Tier 1 — `SSEEvent`, `ToolCall`, `ToolResult` types
- Tier 2 — `decide_tool_mode`, `consume_budget`, `is_terminal_result`
- Tier 3 — `@tool` registration + `ToolRouter.dispatch`

## Run

From the framework monorepo root:

```bash
uv sync
uv run --package steerable-example-py-minimal python -m steerable_example_py_minimal.main
```

Expected output:

```
[harness] mode='read'  budget_exhausted=False  state=BudgetState(tokens_used=120, steps_used=1, tool_calls_used=1)
[runtime] success=True  data={'value': {'path': 'README.md', 'content': '<contents of README.md>'}, 'durationMs': 0}
[wire]    {"type":"tool_result", ... ,"payload":{"callId":"call_1","result":{...}}}
```

## What to look at next

- [Tools spec](../../docs/spec/tools.md) — what `decide_tool_mode` is doing
- [Runtime spec](../../docs/spec/runtime.md) — how `ToolRouter` plugs into the wider runtime
- [`examples/sidecar-roundtrip/`](../sidecar-roundtrip/) — the same flow, but going through the JSON-RPC sidecar
