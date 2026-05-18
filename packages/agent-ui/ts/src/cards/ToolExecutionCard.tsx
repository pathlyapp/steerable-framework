/**
 * `<ToolExecutionCard />`
 *
 * Unified renderer for a single tool / action invocation. Replaces both
 * deeppath's ActionSegment inline strip and deeppath-agent's
 * ExecutedActionsCard: one row per call with status / duration / expandable
 * args + output JSON. The optional `renderArgs` / `renderOutput` slots let an
 * app substitute a custom JSON viewer or rich payload renderer.
 */
import * as React from 'react';
import type { ToolExecutionPayload } from '@steerable/agent-protocol';
import {
  CheckIcon,
  LoaderIcon,
  AlertIcon,
  ZapIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  PlayIcon,
  StopIcon,
} from './icons.js';

type Status = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

const STATUS_LABEL: Record<Status, string> = {
  pending: '待执行',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

const STATUS_TONE: Record<Status, string> = {
  pending: 'text-[var(--agent-muted-foreground,#6b7280)]',
  running: 'text-amber-600',
  succeeded: 'text-emerald-600',
  failed: 'text-rose-600',
  cancelled: 'text-[var(--agent-muted-foreground,#9ca3af)]',
};

export interface ToolExecutionCardProps {
  payload: ToolExecutionPayload;
  className?: string;
  defaultExpanded?: boolean;
  renderArgs?: (args: unknown) => React.ReactNode;
  renderOutput?: (output: unknown) => React.ReactNode;
}

function StatusIcon({ status }: { status: Status }) {
  const cls = STATUS_TONE[status];
  switch (status) {
    case 'running':
      return <LoaderIcon size={12} className={cls} />;
    case 'succeeded':
      return <CheckIcon size={12} className={cls} />;
    case 'failed':
      return <AlertIcon size={12} className={cls} />;
    case 'cancelled':
      return <StopIcon size={12} className={cls} />;
    default:
      return <PlayIcon size={12} className={cls} />;
  }
}

function defaultRender(value: unknown): React.ReactNode {
  if (value === undefined || value === null) return null;
  let text: string;
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <pre className="whitespace-pre-wrap break-words rounded bg-[var(--agent-muted,#f3f4f6)] px-2 py-1.5 text-[11px] text-[var(--agent-foreground,#111827)]">
      {text}
    </pre>
  );
}

export const ToolExecutionCard: React.FC<ToolExecutionCardProps> = ({
  payload,
  className,
  defaultExpanded = false,
  renderArgs,
  renderOutput,
}) => {
  const expandable = payload.expandable !== false;
  const [expanded, setExpanded] = React.useState(defaultExpanded);
  const status = (payload.status ?? 'pending') as Status;

  return (
    <div
      className={[
        'steerable-tool-execution rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-xs',
        className,
      ].filter(Boolean).join(' ')}
    >
      <button
        type="button"
        onClick={() => expandable && setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        aria-expanded={expanded}
        aria-disabled={!expandable}
      >
        {expandable &&
          (expanded ? <ChevronDownIcon size={12} /> : <ChevronRightIcon size={12} />)}
        <ZapIcon size={12} className="text-[var(--agent-muted-foreground,#6b7280)]" />
        <span className="font-medium text-[var(--agent-foreground,#111827)]">{payload.name}</span>
        {payload.summary && (
          <span className="truncate text-[var(--agent-muted-foreground,#6b7280)]">{payload.summary}</span>
        )}
        <span className="ml-auto inline-flex items-center gap-1">
          <StatusIcon status={status} />
          <span className={STATUS_TONE[status]}>{STATUS_LABEL[status]}</span>
          {typeof payload.durationMs === 'number' && (
            <span className="text-[var(--agent-muted-foreground,#9ca3af)]">· {payload.durationMs}ms</span>
          )}
        </span>
      </button>
      {expandable && expanded && (
        <div className="space-y-2 border-t border-[var(--agent-border,#e5e7eb)] px-3 py-2">
          {payload.args !== undefined && (
            <section>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-[var(--agent-muted-foreground,#6b7280)]">
                输入
              </div>
              {renderArgs ? renderArgs(payload.args) : defaultRender(payload.args)}
            </section>
          )}
          {payload.output !== undefined && (
            <section>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-[var(--agent-muted-foreground,#6b7280)]">
                输出
              </div>
              {renderOutput ? renderOutput(payload.output) : defaultRender(payload.output)}
            </section>
          )}
          {payload.error && (
            <section>
              <div className="mb-1 text-[10px] uppercase tracking-wider text-rose-600">错误</div>
              <pre className="whitespace-pre-wrap break-words rounded bg-rose-50/60 px-2 py-1.5 text-[11px] text-rose-700">
                {payload.error}
              </pre>
            </section>
          )}
        </div>
      )}
    </div>
  );
};

export default ToolExecutionCard;
