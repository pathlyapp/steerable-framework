/**
 * `ToolCallRenderer` — display a single tool call + (optional) tool result.
 *
 * Renders three visual rows depending on lifecycle / mode:
 *
 *   ┌──────────────────────────────────────────────────┐
 *   │ [icon] tool_name [mode-pill]              [time] │
 *   │   args (collapsed by default)                    │
 *   │   ── result ──                                   │
 *   │   message + data (collapsed by default)          │
 *   └──────────────────────────────────────────────────┘
 *
 * Headless body: callers can override the args / result subtree with
 * `renderArgs` / `renderResult` if they need richer formatting (diffs, code
 * blocks, structured cards). Default body uses pretty-printed JSON.
 */

import { useState } from 'react';
import type { ToolCall, ToolResult } from '@steerable/agent-protocol';
import { useToolCallStatus, type ToolCallMode } from '../hooks/useToolCallStatus';
import { cn } from './cn';

export interface ToolCallRendererProps {
  call: ToolCall;
  result?: ToolResult;
  /**
   * Optional explicit mode override. Use when the framework's `decide_tool_mode`
   * has already classified the tool (the local `ToolRouter` ships this value
   * alongside the call in production setups).
   */
  mode?: ToolCallMode;
  runningHint?: boolean;
  /**
   * Approval handler. Must be supplied when rendering a `local`-mode call you
   * want the user to confirm before execution. If omitted, the renderer falls
   * back to the read-only "awaiting result" treatment.
   */
  onApprove?: (call: ToolCall) => void;
  onReject?: (call: ToolCall) => void;
  className?: string;
  renderArgs?: (call: ToolCall) => React.ReactNode;
  renderResult?: (result: ToolResult) => React.ReactNode;
  defaultExpanded?: boolean;
}

const modeBadgeClass: Record<ToolCallMode, string> = {
  read: 'bg-agent-tool-read/15 text-agent-tool-read border-agent-tool-read/40',
  safe_write:
    'bg-agent-tool-write/15 text-agent-tool-write border-agent-tool-write/40',
  destructive:
    'bg-agent-tool-destructive/15 text-agent-tool-destructive border-agent-tool-destructive/40',
  local:
    'bg-agent-accent/15 text-agent-accent border-agent-accent/40',
  external:
    'bg-agent-muted text-agent-muted-foreground border-agent-border',
  ui: 'bg-agent-muted text-agent-muted-foreground border-agent-border',
  synthetic:
    'bg-agent-muted text-agent-muted-foreground border-agent-border',
  unknown:
    'bg-agent-muted text-agent-muted-foreground border-agent-border',
};

const statusDotClass: Record<string, string> = {
  pending: 'bg-agent-muted-foreground animate-pulse',
  running: 'bg-agent-accent animate-pulse',
  done: 'bg-agent-tool-read',
  error: 'bg-agent-destructive',
};

function defaultArgsRenderer(call: ToolCall) {
  return (
    <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-agent-muted-foreground">
      {safeStringify(call.arguments)}
    </pre>
  );
}

function defaultResultRenderer(result: ToolResult) {
  const lines: React.ReactNode[] = [];
  if (result.message) {
    lines.push(
      <div key="msg" className="text-xs text-agent-foreground">
        {result.message}
      </div>,
    );
  }
  if (result.error) {
    lines.push(
      <div key="err" className="text-xs text-agent-destructive">
        {result.error}
      </div>,
    );
  }
  if (result.data) {
    lines.push(
      <pre
        key="data"
        className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-agent-muted-foreground"
      >
        {safeStringify(result.data)}
      </pre>,
    );
  }
  if (lines.length === 0) {
    lines.push(
      <div key="ok" className="text-xs text-agent-muted-foreground">
        ok
      </div>,
    );
  }
  return <div className="space-y-1">{lines}</div>;
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function ToolCallRenderer(props: ToolCallRendererProps) {
  const {
    call,
    result,
    mode,
    runningHint,
    onApprove,
    onReject,
    className,
    renderArgs = defaultArgsRenderer,
    renderResult = defaultResultRenderer,
    defaultExpanded = false,
  } = props;
  const [expanded, setExpanded] = useState(defaultExpanded);

  const status = useToolCallStatus({ call, result, mode, runningHint });

  return (
    <div
      data-status={status.status}
      data-mode={status.mode}
      className={cn(
        'rounded-agent-md border bg-agent-canvas px-3 py-2 text-agent-foreground',
        status.isDestructive ? 'border-agent-destructive/40' : 'border-agent-border',
        className,
      )}
    >
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 text-left"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span
            className={cn('h-2 w-2 rounded-full', statusDotClass[status.status])}
            aria-hidden
          />
          <span className="truncate font-mono text-xs font-medium">
            {call.name}
          </span>
          <span
            className={cn(
              'shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
              modeBadgeClass[status.mode],
            )}
          >
            {status.mode}
          </span>
        </span>
        <span className="shrink-0 text-[10px] uppercase tracking-wide text-agent-muted-foreground">
          {status.status}
        </span>
      </button>
      {status.requiresApproval && !result ? (
        <div className="mt-2 flex items-center gap-2">
          <span className="text-xs text-agent-muted-foreground">
            Local execution requires approval.
          </span>
          {onApprove ? (
            <button
              type="button"
              className="rounded-agent-sm bg-agent-accent px-2 py-1 text-xs font-medium text-agent-accent-foreground"
              onClick={() => onApprove(call)}
            >
              Approve
            </button>
          ) : null}
          {onReject ? (
            <button
              type="button"
              className="rounded-agent-sm border border-agent-border px-2 py-1 text-xs font-medium text-agent-foreground"
              onClick={() => onReject(call)}
            >
              Reject
            </button>
          ) : null}
        </div>
      ) : null}
      {expanded ? (
        <div className="mt-2 space-y-2 border-t border-agent-border pt-2">
          {renderArgs(call)}
          {result ? (
            <div className="border-t border-dashed border-agent-border pt-2">
              {renderResult(result)}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
