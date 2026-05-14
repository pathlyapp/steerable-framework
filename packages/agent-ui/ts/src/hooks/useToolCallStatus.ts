/**
 * `useToolCallStatus` — derive the rendering state for a single tool call.
 *
 * Tool calls in the framework go through four UI states:
 *   `pending`    The model has emitted the call but no result has arrived.
 *   `running`    Optional intermediate state when the runtime emits a
 *                `tool.running` notification (sidecar / harness can do this).
 *   `done`       A successful result has been attached.
 *   `error`      The result reports `success: false` or an error envelope.
 *
 * Plus an additional axis — the `mode` (read / safe_write / destructive / local)
 * — which decides whether the UI should render an approval prompt or an
 * informational badge.
 */

import { useMemo } from 'react';
import type { ToolCall, ToolResult } from '@steerable/agent-protocol';

export type ToolCallStatus = 'pending' | 'running' | 'done' | 'error';

export type ToolCallMode =
  | 'read'
  | 'safe_write'
  | 'destructive'
  | 'local'
  | 'external'
  | 'ui'
  | 'synthetic'
  | 'unknown';

export interface UseToolCallStatusOptions {
  call: ToolCall;
  result?: ToolResult;
  /**
   * Optional override; when omitted the hook infers the mode from the tool
   * name pattern (`get_*` / `list_*` → read, `delete_*` / `archive_*` →
   * destructive, `local_*` → local, etc.). Mirrors the framework's harness
   * `decide_tool_mode`.
   */
  mode?: ToolCallMode;
  /**
   * If the runtime can emit `tool.running` notifications, set this true to
   * keep `status` in `running` until a result arrives. Defaults to false
   * (status flips straight from `pending` → `done`/`error`).
   */
  runningHint?: boolean;
}

export interface UseToolCallStatusReturn {
  status: ToolCallStatus;
  mode: ToolCallMode;
  /** True when the UI should ask for explicit user approval before running. */
  requiresApproval: boolean;
  /**
   * True when the runtime should mark the call as destructive in the UI
   * (red tone, undo affordance).
   */
  isDestructive: boolean;
}

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

export function useToolCallStatus(
  options: UseToolCallStatusOptions,
): UseToolCallStatusReturn {
  return useMemo<UseToolCallStatusReturn>(() => {
    const mode = options.mode ?? inferMode(options.call.name);
    const isDestructive = mode === 'destructive';
    // `local` tools touch the user's machine and need explicit consent.
    const requiresApproval = mode === 'local';
    let status: ToolCallStatus;
    if (!options.result) {
      status = options.runningHint ? 'running' : 'pending';
    } else if (options.result.success === false) {
      status = 'error';
    } else {
      status = 'done';
    }
    return { status, mode, requiresApproval, isDestructive };
  }, [options.call.name, options.mode, options.result, options.runningHint]);
}
