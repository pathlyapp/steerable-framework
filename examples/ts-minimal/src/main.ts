import type { SSEEvent, ToolCall, ToolResult } from '@steerable/agent-protocol';
import {
  consumeBudget,
  decideToolMode,
  isTerminalResult,
} from '@steerable/agent-harness';

const call: ToolCall = {
  id: 'call_1',
  name: 'read_file',
  arguments: { path: 'README.md' },
};

const mode = decideToolMode(call.name); // 'read'

const { state, exhausted } = consumeBudget(
  { tokensUsed: 0, stepsUsed: 0, toolCallsUsed: 0 },
  { maxTokens: 5_000, maxSteps: 30, maxToolCalls: 10 },
  { toolCall: true, tokens: 120, step: true },
);

const result: ToolResult = {
  success: true,
  message: `mode=${mode}, exhausted=${exhausted}`,
  data: { state },
};

const done = isTerminalResult(result);

const wire: SSEEvent = {
  type: done ? 'done' : 'tool_result',
  payload: { callId: call.id, result },
};

console.log(`[harness] mode=${mode}  exhausted=${exhausted}  state=${JSON.stringify(state)}`);
console.log(`[wire]    ${JSON.stringify(wire)}`);
