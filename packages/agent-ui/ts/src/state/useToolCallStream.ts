/**
 * `useToolCallStream` — turn the assistant message's `toolCalls` / `toolResult`
 * (as maintained by `useChatStream`) into a derived `{ id -> {call, result,
 * status, mode} }` map, so renderers can show per-call cards without
 * threading status through the tree manually.
 *
 * Aggregates `useToolCallStatus` across all tool calls on a single message.
 * Pure derivation, no I/O.
 */

import { useMemo } from 'react';
import type { ChatMessage, ToolCall, ToolResult } from '@steerable/agent-protocol';
import {
  useToolCallStatus,
  type ToolCallMode,
  type ToolCallStatus,
} from '../hooks/useToolCallStatus.js';

export interface ToolCallEntry {
  call: ToolCall;
  result?: ToolResult;
  status: ToolCallStatus;
  mode: ToolCallMode;
  isDestructive: boolean;
  requiresApproval: boolean;
}

export interface UseToolCallStreamOptions {
  message: ChatMessage | null | undefined;
  /**
   * Optional per-call result lookup — when the runtime emits `tool_result`
   * with a `callId`, we can pair it to the originating call rather than
   * relying on `message.toolResult` (which always holds the last result).
   */
  resultByCallId?: Record<string, ToolResult>;
  /** Optional mode override per tool name. */
  modeByName?: Record<string, ToolCallMode>;
}

export interface UseToolCallStreamReturn {
  entries: ToolCallEntry[];
  pendingCount: number;
  errorCount: number;
}

export function useToolCallStream(
  options: UseToolCallStreamOptions,
): UseToolCallStreamReturn {
  const message = options.message ?? null;
  return useMemo(() => {
    if (!message || !Array.isArray(message.toolCalls)) {
      return { entries: [], pendingCount: 0, errorCount: 0 };
    }
    const entries: ToolCallEntry[] = message.toolCalls.map((call) => {
      const lookup = options.resultByCallId?.[call.id];
      const fallback =
        message.toolResult && (message.toolResult as { callId?: string }).callId === call.id
          ? message.toolResult
          : undefined;
      const result = lookup ?? fallback ?? message.toolResult;
      const status = deriveStatus(result);
      const mode = options.modeByName?.[call.name] ?? inferMode(call.name);
      return {
        call,
        result,
        status,
        mode,
        isDestructive: mode === 'destructive',
        requiresApproval: mode === 'local',
      };
    });
    return {
      entries,
      pendingCount: entries.filter((e) => e.status === 'pending').length,
      errorCount: entries.filter((e) => e.status === 'error').length,
    };
  }, [message, options.resultByCallId, options.modeByName]);
}

// `useToolCallStatus` is per-call; this hook is the bulk variant. We keep the
// inference rules in sync by re-using the same heuristics here.

const READ_PATTERNS = [/^get[_-]/, /^list[_-]/, /^read[_-]/, /^search[_-]/];
const DESTRUCTIVE_PATTERNS = [
  /^delete[_-]/,
  /^remove[_-]/,
  /^archive[_-]/,
  /^purge[_-]/,
  /^drop[_-]/,
];
const SAFE_WRITE_PATTERNS = [/^create[_-]/, /^update[_-]/, /^add[_-]/, /^set[_-]/];
const LOCAL_PATTERNS = [/^local[_-]/, /^shell[_-]/, /^exec[_-]/];

function inferMode(name: string): ToolCallMode {
  const lower = name.toLowerCase();
  if (READ_PATTERNS.some((re) => re.test(lower))) return 'read';
  if (LOCAL_PATTERNS.some((re) => re.test(lower))) return 'local';
  if (DESTRUCTIVE_PATTERNS.some((re) => re.test(lower))) return 'destructive';
  if (SAFE_WRITE_PATTERNS.some((re) => re.test(lower))) return 'safe_write';
  return 'unknown';
}

function deriveStatus(result: ToolResult | undefined): ToolCallStatus {
  if (!result) return 'pending';
  if (result.success === false) return 'error';
  return 'done';
}

// Re-export the per-call hook for convenience.
export { useToolCallStatus } from '../hooks/useToolCallStatus.js';
