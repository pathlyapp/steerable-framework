# TypeScript Minimal Example

Small example showing protocol + harness usage together.

## Install

```bash
pnpm add @steerable/agent-protocol @steerable/agent-harness
```

## Example

```ts
import type { ToolCall, ToolResult, SSEEvent } from "@steerable/agent-protocol";
import { decideToolMode, consumeBudget, isTerminalResult } from "@steerable/agent-harness";

const call: ToolCall = {
  id: "call_1",
  name: "read_file",
  arguments: { path: "README.md" },
};

const mode = decideToolMode(call.name); // "read"

const { state, exhausted } = consumeBudget(
  { tokensUsed: 0, stepsUsed: 0, toolCallsUsed: 0 },
  { maxTokens: 5000, maxSteps: 30, maxToolCalls: 10 },
  { toolCall: true, tokens: 120, step: true }
);

const result: ToolResult = {
  success: true,
  message: `mode=${mode}, exhausted=${exhausted}`,
  data: { state },
};

const done = isTerminalResult(result);

const streamEvent: SSEEvent = {
  type: done ? "done" : "tool_result",
  payload: { callId: call.id, result },
};
```

Use this pattern as a thin foundation before adding orchestration logic.
