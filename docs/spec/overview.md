# Spec Overview

Steerable follows a **single schema, multi-language** workflow:

1. Define JSON Schemas in `spec/`
2. Generate TypeScript interfaces (+ Zod) and Python Pydantic models
3. Verify behavior parity with conformance tests against
   `tests/conformance/cases/`

## Schema folders

| Folder              | Models                                                                   |
| ------------------- | ------------------------------------------------------------------------ |
| `spec/events/`      | `SSEEvent` (streaming envelope, see [Events](events.md))                 |
| `spec/tools/`       | `ToolCall`, `ToolResult` (see [Tools](tools.md))                         |
| `spec/chat/`        | `ChatMessage`, `ChatAgent` (see [Chat](chat.md))                         |
| `spec/safety/`      | `CommandSafetyPattern` (see [Safety](safety.md))                         |
| `spec/runtime/`     | `AgentSession`, `HarnessTrace`, `TraceSpan`, `TraceEvent` ([Runtime](runtime.md)) |
| `spec/sidecar/`     | `SidecarRequest`, `SidecarResponse`, `SidecarNotification`, `SidecarHealth`, `SidecarError` ([Sidecar](sidecar.md)) |

## Generation pipeline

```bash
pnpm gen          # writes packages/agent-protocol/ts/src/generated/*.ts
uv run python scripts/generate_py.py
                  # writes packages/agent-protocol/py/src/steerable_agent_protocol/generated.py
```

## Drift detection

```bash
pnpm check:drift           # TypeScript: re-runs generator and diffs output
uv run python scripts/check_drift.py    # Python equivalent
```

Both run in CI on every PR; a hand-edited generated file fails the build.

## Lock-step versioning

**All 7 publishable packages** (TS protocol/harness/ui + Py protocol/harness/runtime/sidecar) are kept at the same version by `scripts/check_lockstep_versions.py` (CI-enforced on every tag push). Releases are operator-driven: `./scripts/release/bump_to.sh X.Y.Z` is the only writer of versions, and the lockstep gate in `.github/workflows/release.yml` refuses any tag whose source tree disagrees with the tag.

This is stricter than the original protocol-only lockstep — TS and Py implementations of `agent-protocol` (the codegen pair) plus the four other packages all move together. The cost is a few extra registry versions on no-op packages per release; the benefit is partial-bump corruption is structurally impossible.

## Forward / backward compatibility rules

- Adding a new optional field is **non-breaking** — bump patch.
- Adding a new value to a typed enum (e.g. a new `SSEEvent.type`) is
  **minor** — old consumers should ignore unknown variants.
- Removing a field, or making an optional field required, is
  **breaking** — major bump only.
- New schemas (entire new model files) are minor.

The TypeScript codegen treats every event payload as having
`additionalProperties: true` so future event types in the wire stream
won't crash a stale consumer.
