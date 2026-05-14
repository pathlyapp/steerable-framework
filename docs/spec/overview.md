# Spec Overview

Steerable follows a **single schema, multi-language** workflow:

1. Define JSON Schemas in `spec/`
2. Generate TypeScript interfaces and Python models
3. Verify behavior parity with conformance tests

## Spec folders

- `spec/events/`: streaming event envelope (`SSEEvent`)
- `spec/tools/`: tool call + tool result (`ToolCall`, `ToolResult`)
- `spec/chat/`: chat and agent metadata (`ChatMessage`, `ChatAgent`)
- `spec/safety/`: command safety rule model (`CommandSafetyPattern`)

## Generation pipeline

- TS generation script: `scripts/generate_ts.mjs`
- PY generation script: `scripts/generate_py.py`
- Drift checks: `scripts/check_ts_drift.mjs`, `scripts/check_drift.py`

Run from repo root:

```bash
pnpm gen
pnpm check:drift
```
