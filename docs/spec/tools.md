# Tools Spec

Tool interaction is modeled as a pair:

- `ToolCall`: what the assistant asks to run
- `ToolResult`: what the runtime reports back

## ToolCall

Required fields:

- `id: string`
- `name: string`
- `arguments: object`

`additionalProperties` is disabled, so call envelopes stay predictable.

## ToolResult

Required fields:

- `success: boolean`

Common optional fields:

- `terminal`: explicitly marks terminal tool outcome
- `needsFollowup`: requests another assistant step
- `nextAction`: machine-readable hint for next operation
- `message` / `error`: user-facing and debug text
- `data`: extensible result payload

`ToolResult` allows additional properties for forward compatibility.

## Completion behavior

Harness helpers (`isTerminalResult` / `is_terminal_result`) interpret result state
with this practical rule:

- terminal if `terminal == true`
- terminal if `success == false` and `needsFollowup != true`
