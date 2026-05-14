# Steerable Spec

Cross-language contract source of truth.

Folders:

- `events/`: SSE payload envelopes and event types.
- `tools/`: tool call / result.
- `chat/`: chat message and chat agent models.
- `safety/`: command safety rules.
- `runtime/`: persistent runtime state — agent sessions, harness traces, spans, events.
- `sidecar/`: stdio JSON-RPC wire protocol for the steerable-sidecar.

All `.schema.json` files are consumed by the TypeScript and Python generators
(`scripts/generate_ts.mjs`, `scripts/generate_py.py`). Drift is enforced in CI by
`scripts/check_ts_drift.mjs` and `scripts/check_drift.py`.

Tier mapping:

| Tier | Spec scope | Owning package(s) |
| --- | --- | --- |
| 1. Protocol | `events/`, `tools/`, `chat/`, `safety/` | `@steerable/agent-protocol` (TS) + `steerable-agent-protocol` (Py) |
| 2. Harness | (consumes Tier 1) | `steerable-agent-harness` (Py) |
| 3. Runtime | `runtime/`, `sidecar/` | `steerable-agent-runtime` (Py) + `steerable-sidecar` (Py) |
| 4. UI | (consumes Tier 1 only, types-only) | `@steerable/agent-ui` (TS) |
