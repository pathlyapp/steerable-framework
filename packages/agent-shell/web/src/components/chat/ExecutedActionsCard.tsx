import type { ReactNode } from 'react';
import { LuCircleCheck, LuCircleX } from 'react-icons/lu';
import { ToolExecutionCard } from '@steerable/agent-ui/cards';
import type { ToolExecutionPayload } from '@steerable/agent-protocol';
import { UnifiedDiff } from './UnifiedDiff';
import { summarizeRunCodeAction, summarizeWebAction, expandRunCodeActions } from './executed-actions-model';

/**
 * ExecutedActionsCard — visual surface for the `executed_actions` SSE event
 * emitted by local-backend after each tool-call round.
 *
 * Per `src/local-backend/router.ts` each entry is:
 *   { tool, mode, policy, shellClassification, arguments, result }
 *
 * The list rendering is delegated to `@steerable/agent-ui/cards`'
 * `ToolExecutionCard`; this file is the thin local adapter that:
 *   1. Maps the local-backend `ExecutedAction` shape to the framework's
 *      canonical `ToolExecutionPayload` (id / name / status / args / output).
 *   2. Adds the "已自动执行 N 个操作" summary banner above the list — the
 *      framework intentionally leaves grouping policy to the host app since
 *      different products count successes / failures differently.
 */

export interface ExecutedAction {
  /** Sidecar tool-call id; used to update a running row when the result lands. */
  id?: string;
  tool: string;
  mode?: string;
  policy?: unknown;
  shellClassification?: string;
  arguments?: unknown;
  result?: unknown;
  /**
   * W4-2: per-exec sandbox marker (`data._sandbox` lifted by the CoreLoop).
   * enforcement: full = OS deny-by-default; partial = documented gap (e.g.
   * port-only egress); none = no backend on this platform.
   */
  sandbox?: { backend?: string; enforcement: string };
}

interface ExecutedActionsCardProps {
  actions: ExecutedAction[];
  /** Drop the outer card chrome when nested in a turn-process group. */
  compact?: boolean;
  /** Show args/output instead of a one-line summary. Defaults to collapsed. */
  defaultExpanded?: boolean;
}

function deriveStatus(action: ExecutedAction): ToolExecutionPayload['status'] {
  const result = action.result;
  if (result === undefined || result === null) return 'running';
  if (typeof result !== 'object') return 'succeeded';
  if ('success' in result) {
    return (result as { success?: unknown }).success ? 'succeeded' : 'failed';
  }
  if ('error' in result) return 'failed';
  return 'succeeded';
}

function summarizeArguments(args: unknown): string | null {
  if (args === null || args === undefined) return null;
  if (typeof args === 'string') return args.length > 60 ? args.slice(0, 57) + '…' : args;
  if (typeof args !== 'object') return String(args);
  const obj = args as Record<string, unknown>;
  for (const key of ['command', 'cmd', 'query', 'path', 'file', 'target', 'message']) {
    const v = obj[key];
    if (typeof v === 'string' && v.length > 0) {
      return v.length > 60 ? v.slice(0, 57) + '…' : v;
    }
  }
  return null;
}

function sandboxBadge(sandbox: ExecutedAction['sandbox']): string | null {
  if (!sandbox) return null;
  switch (sandbox.enforcement) {
    case 'full':
      return '[沙箱]';
    case 'partial':
      return '[沙箱·部分]';
    default:
      return '[未沙箱]';
  }
}

function actionToTool(action: ExecutedAction, idx: number): ToolExecutionPayload {
  const status = deriveStatus(action);
  const errorText =
    status === 'failed' && action.result && typeof action.result === 'object'
      ? typeof (action.result as { error?: unknown }).error === 'string'
        ? ((action.result as { error: string }).error)
        : null
      : null;
  // W5-2: web_search/web_fetch 用结构化中文摘要（查询词/URL + 结果计数/
  // 状态码），其余工具走通用参数摘要。
  const summary =
    summarizeRunCodeAction(action.tool, action.arguments, action.result) ??
    summarizeWebAction(action.tool, action.arguments, action.result) ??
    summarizeArguments(action.arguments);
  const badge = sandboxBadge(action.sandbox);
  return {
    id: action.id ?? `${action.tool}-${idx}`,
    name: action.tool,
    status,
    summary: badge ? `${badge} ${summary ?? ''}`.trim() : summary,
    args: action.arguments,
    output: action.result,
    error: errorText,
    durationMs: null,
    icon: null,
    expandable: true,
  };
}

/**
 * renderOutput slot for ToolExecutionCard: when a tool result carries a
 * unified `diff` (local_edit_file), render it as a coloured diff; otherwise
 * fall back to the default JSON/text view.
 */
function renderActionOutput(output: unknown): ReactNode {
  if (output && typeof output === 'object') {
    const obj = output as Record<string, unknown>;
    if (typeof obj.diff === 'string' && obj.diff.length > 0) {
      const { diff, ...rest } = obj;
      const hasRest = Object.keys(rest).some(
        (k) => k !== 'success' && rest[k] !== undefined && rest[k] !== null,
      );
      return (
        <div className="space-y-2">
          <UnifiedDiff diff={diff} />
          {hasRest && (
            <pre className="whitespace-pre-wrap break-words rounded bg-agent-muted/40 px-2 py-1.5 text-[11px] text-agent-foreground">
              {JSON.stringify(rest, null, 2)}
            </pre>
          )}
        </div>
      );
    }
  }
  if (output === undefined || output === null) return null;
  const text = typeof output === 'string' ? output : JSON.stringify(output, null, 2);
  return (
    <pre className="whitespace-pre-wrap break-words rounded bg-agent-muted/40 px-2 py-1.5 text-[11px] text-agent-foreground">
      {text}
    </pre>
  );
}

/** Inline tool rows for a mixed think→act timeline (no summary banner). */
export function ToolsFlow({
  actions,
  compact = false,
  defaultExpanded = false,
}: ExecutedActionsCardProps) {
  if (!actions || actions.length === 0) return null;
  const expanded = expandRunCodeActions(actions);
  return (
    <div
      className={
        compact
          ? 'overflow-hidden rounded-agent-md border border-agent-border/80 bg-agent-canvas'
          : 'my-2 overflow-hidden rounded-agent-md border border-agent-border bg-agent-canvas'
      }
    >
      <div className="space-y-px">
        {expanded.map((action, i) => (
          <ToolExecutionCard
            key={action.id ?? `${action.tool}-${i}`}
            payload={actionToTool(action, i)}
            defaultExpanded={defaultExpanded}
            renderOutput={renderActionOutput}
            className="rounded-none border-0 border-t border-agent-border first:border-t-0"
          />
        ))}
      </div>
    </div>
  );
}

export function ExecutedActionsCard({ actions }: ExecutedActionsCardProps) {
  if (!actions || actions.length === 0) return null;

  const expanded = expandRunCodeActions(actions);
  const successCount = expanded.filter((a) => deriveStatus(a) === 'succeeded').length;
  const failureCount = expanded.filter((a) => deriveStatus(a) === 'failed').length;
  const runningCount = expanded.filter((a) => deriveStatus(a) === 'running').length;

  return (
    <div className="my-2 overflow-hidden rounded-agent-md border border-agent-border bg-agent-canvas">
      <div className="flex items-center justify-between border-b border-agent-border bg-agent-muted/30 px-3 py-1.5">
        <div className="flex items-center gap-1.5 text-xs">
          {failureCount === 0 ? (
            <LuCircleCheck className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
          ) : (
            <LuCircleX className="h-3.5 w-3.5 text-agent-destructive" />
          )}
          <span className="font-medium text-agent-foreground">
            已自动执行 {expanded.length} 个操作
          </span>
        </div>
        <span className="text-[11px] text-agent-muted-foreground">
          {runningCount > 0
            ? `${runningCount} 执行中`
            : failureCount > 0
              ? `${successCount} 成功 · ${failureCount} 失败`
              : `全部成功`}
        </span>
      </div>
      <div className="space-y-px">
        {expanded.map((action, i) => (
          <ToolExecutionCard
            key={action.id ?? `${action.tool}-${i}`}
            payload={actionToTool(action, i)}
            renderOutput={renderActionOutput}
            className="rounded-none border-0 border-t border-agent-border first:border-t-0"
          />
        ))}
      </div>
    </div>
  );
}

export default ExecutedActionsCard;
