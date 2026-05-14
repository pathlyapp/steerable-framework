# Events Spec

`SSEEvent` is the universal stream envelope for agent runtime updates.
Every event flowing from the runtime to a UI client (whether over HTTP
SSE, Electron IPC, or sidecar `stream.chunk` notifications) is a member
of this union.

## Schema

| Field                    | Type                       | Notes                                  |
| ------------------------ | -------------------------- | -------------------------------------- |
| `type`                   | `string` (enum, required)  | One of the variants below              |
| `event`                  | `string`                   | Optional raw SSE event name (legacy)   |
| `content`                | `string`                   | Streamed text payload (`type: content`) |
| `hint`                   | `string`                   | Loader hint (`type: loader-hint`)      |
| `message`                | `string`                   | Error / budget message                 |
| `code`                   | `string`                   | Machine-readable error code            |
| `orchestrationGroupId`   | `string`                   | Correlation ID for multi-agent runs    |
| `taskId` / `messageId`   | `string`                   | Per-task / per-message correlation     |
| `payload`                | `Record<string, unknown>`  | Variant-specific structured data       |

`additionalProperties` is **true** at the envelope level so consumers can
forward unknown fields without crashing on framework upgrades.

## `type` variants

| Variant              | Direction        | Meaning                                                                 |
| -------------------- | ---------------- | ----------------------------------------------------------------------- |
| `content`            | runtime → client | Incremental token (`content` field carries the delta)                   |
| `tool_call`          | runtime → client | Assistant requested a tool (`payload`: `ToolCall`)                      |
| `tool_result`        | runtime → client | Tool finished (`payload`: `ToolResult`)                                 |
| `error`              | runtime → client | Recoverable error mid-stream (`message`, optional `code`)               |
| `budget_exhausted`   | runtime → client | Harness stopped the run (`message` describes which limit fired)         |
| `agent`              | runtime → client | Multi-agent metadata (which agent owns the upcoming bubble, etc.)       |
| `orchestration`      | runtime → client | Coordinator plan / status updates (richer payload, see Runtime spec)    |
| `loader-hint`        | runtime → client | Free-form "next step" hint shown in placeholder bubbles                 |
| `keepalive`          | runtime → client | Heartbeat (no payload)                                                  |
| `done`               | runtime → client | Stream terminator                                                       |

## Wire format

When transported over HTTP SSE:

```
event: message
data: {"type":"content","content":"Hello "}

event: message
data: {"type":"content","content":"world!"}

event: message
data: {"type":"tool_call","payload":{"id":"c1","name":"read_file","arguments":{"path":"README.md"}}}

event: message
data: {"type":"tool_result","payload":{"success":true,"data":{"content":"…"}}}

event: message
data: [DONE]
```

`[DONE]` is the canonical SSE terminator; the framework also accepts
`{"type":"done"}` for transports that prefer JSON.

When transported over the sidecar's `stream.chunk` notifications, the
same payload appears in the notification's `params.delta` /
`params.toolCall` / `params.usage` fields — see [Sidecar spec](sidecar.md).

## Examples

A token stream with a tool call mid-flight:

```json
[
  {"type":"content","content":"I'll check the docs. "},
  {"type":"tool_call","payload":{"id":"c1","name":"read_file","arguments":{"path":"README.md"}}},
  {"type":"tool_result","payload":{"success":true,"data":{"content":"# Steerable…"}}},
  {"type":"content","content":"Here's the summary: …"},
  {"type":"done"}
]
```

A budget-exhausted stop:

```json
{"type":"budget_exhausted","message":"max_tool_calls=10 reached","code":"E_BUDGET_TOOL_CALLS"}
```
