# Tools Spec

Tool interaction is modeled as two strict types:

- `ToolCall` — what the assistant asks to run
- `ToolResult` — what the runtime reports back

Plus one orthogonal classifier — `ToolMode` — that the harness uses to
decide whether a call needs explicit user consent.

## ToolCall

| Field        | Type                       | Required | Notes                                      |
| ------------ | -------------------------- | -------- | ------------------------------------------ |
| `id`         | `string`                   | yes      | Unique within a chat (use cuid2 or similar) |
| `name`       | `string`                   | yes      | Tool name registered with the runtime      |
| `arguments`  | `Record<string, unknown>`  | yes      | LLM-provided JSON args (validated by tool's schema) |

`additionalProperties` is **disabled** so tool envelopes stay
deterministic across versions. New per-call metadata should go through
the harness's `TraceSpan.attrs`, not into `ToolCall`.

## ToolResult

| Field           | Type                       | Required | Notes                                          |
| --------------- | -------------------------- | -------- | ---------------------------------------------- |
| `success`       | `boolean`                  | yes      | Hard distinction — `false` flips status to error |
| `terminal`      | `boolean`                  | no       | Explicitly mark the result as terminal         |
| `needsFollowup` | `boolean`                  | no       | Even on `success: false`, re-prompt the LLM    |
| `nextAction`    | `string`                   | no       | Machine-readable hint for the next operation   |
| `message`       | `string`                   | no       | User-facing text (rendered in the bubble)      |
| `error`         | `string`                   | no       | Debug-friendly error string (logged + shown)   |
| `data`          | `Record<string, unknown>`  | no       | Arbitrary structured payload                   |

`additionalProperties` is **enabled** for forward compatibility.

## ToolMode (harness classifier)

The harness's [`decide_tool_mode(name)`](../spec/architecture.md) returns
one of:

| Mode          | Meaning                              | Default UI treatment       |
| ------------- | ------------------------------------ | -------------------------- |
| `read`        | Pure inspection (no side effects)    | Auto-run, no consent       |
| `safe_write`  | Bounded mutation (e.g. update_event) | Auto-run with diff preview |
| `destructive` | Irreversible (delete_*, drop_*, …)   | Auto-run, undo affordance  |
| `local`       | Touches the user's machine           | **Requires consent**       |
| `external`    | Calls outside services               | Auto-run, log              |

Pattern rules (TypeScript regex equivalents in
`@steerable/agent-ui/useToolCallStatus`):

```
^get_  | ^list_  | ^read_  | ^search_   →  read
^create_ | ^update_ | ^add_ | ^set_     →  safe_write
^delete_ | ^remove_ | ^archive_ | ^drop_ →  destructive
^local_ | ^shell_ | ^exec_              →  local
```

You can override the inferred mode at registration time via the `@tool`
decorator's `mode=` kwarg.

## Completion semantics

`isTerminalResult(result)` (TS) /
`is_terminal_result(result.model_dump())` (Py) treats a result as
terminal when:

- `terminal == true`, **or**
- `success == false` **and** `needsFollowup != true`

Use `needsFollowup=True` on a failure to ask the LLM to self-heal (write
a different argument, try a different tool, etc.). Without it, a failed
call ends the run.

## Example pair

```json
// ToolCall
{"id":"c_42","name":"create_event","arguments":{"title":"Lunch","start":"2026-05-15T12:00:00Z"}}

// ToolResult (success)
{"success":true,"message":"Event created.","data":{"eventId":"e_777"}}

// ToolResult (recoverable failure)
{"success":false,"needsFollowup":true,"error":"Invalid date format","message":"Please retry with ISO-8601."}

// ToolResult (terminal failure)
{"success":false,"terminal":true,"error":"Calendar service unavailable"}
```
