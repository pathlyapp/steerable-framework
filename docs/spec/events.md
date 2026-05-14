# Events Spec

`SSEEvent` is the standard stream envelope for agent runtime updates.

## Required field

- `type: string`

Allowed `type` values are currently:

- `content`, `error`, `agent`, `orchestration`
- `loader-hint`, `keepalive`, `done`, `budget_exhausted`
- `tool_call`, `tool_result`

## Common optional fields

- `event`: raw SSE event name
- `content`: streamed text payload
- `message` / `code`: error metadata
- `taskId`, `messageId`, `orchestrationGroupId`: correlation IDs
- `payload`: extensible object for event-specific data

## Example

```json
{
  "type": "tool_result",
  "taskId": "task_123",
  "messageId": "msg_456",
  "payload": {
    "tool": "read_file",
    "success": true
  }
}
```
