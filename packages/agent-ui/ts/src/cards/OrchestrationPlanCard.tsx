/**
 * `<OrchestrationPlanCard />`
 *
 * The visible surface of an otherwise invisible Coordinator agent. Renders
 * one row per worker task with a status dot driven by `taskStatuses` (a map
 * the parent computes off sibling assistant bubbles tagged with the same
 * orchestrationGroupId + orchestrationTaskId).
 *
 * Three top-level shells are supported via mutually-exclusive flags so the
 * Coordinator can express its lifecycle without forcing the caller to render
 * three different components:
 *   - `loading` + optional `loadingHint`   — Coordinator is still planning.
 *   - `failed` + optional `failure`        — planning errored.
 *   - default                              — `payload.tasks` are rendered.
 *
 * Slots:
 *   - `renderAbove` / `renderBelowHeader`  — out-of-flow rails for badges and
 *      context-load lines that deeppath wants to ride along.
 *   - `renderTaskRow(task, status)`        — full row override (deeppath uses
 *      it to draw the per-agent colored chip + monospaced task id + right-
 *      aligned status label).
 *   - `headerLabel`, `headerSecondary`     — override the default
 *      "编排计划 · N 个子任务" header label/subline.
 *   - `hideMode`, `hideHeaderCounts`       — drop the optional header chips
 *      when the host UI does not want them (deeppath hides both).
 *   - controlled `expanded` + `onExpandedChange`.
 */
import * as React from 'react';
import type { OrchestrationPlanPayload } from '@steerable/agent-protocol';
import {
  ChevronDownIcon,
  ChevronRightIcon,
  GitBranchIcon,
  LoaderIcon,
  AlertIcon,
} from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

export type OrchestrationTaskStatus =
  | 'pending'
  | 'running'
  | 'ok'
  | 'skipped'
  | 'failed'
  | 'ask_user_paused';

export type OrchestrationTask = OrchestrationPlanPayload['tasks'][number];

export interface OrchestrationPlanCardProps {
  payload: OrchestrationPlanPayload;
  /** Status of each task, keyed by `payload.tasks[i].id`. Missing → pending. */
  taskStatuses?: Record<string, OrchestrationTaskStatus>;
  /** Lookup of agentId → display name; falls back to the raw id. */
  agentNameFor?: (agentId: string) => string;
  className?: string;
  defaultExpanded?: boolean;
  expanded?: boolean;
  onExpandedChange?: (next: boolean) => void;

  // Lifecycle shells -- mutually exclusive with each other & the default body
  loading?: boolean;
  loadingHint?: React.ReactNode;
  failed?: boolean;
  failure?: React.ReactNode;

  // Header customisation
  headerLabel?: React.ReactNode;
  headerSecondary?: React.ReactNode;
  hideMode?: boolean;
  hideHeaderCounts?: boolean;

  // Out-of-flow rails
  renderAbove?: () => RenderSlotResult;
  renderBelowHeader?: () => RenderSlotResult;

  // Per-row override
  renderTaskRow?: (task: OrchestrationTask, status: OrchestrationTaskStatus) => RenderSlotResult;
}

const STATUS_LABEL: Record<OrchestrationTaskStatus, string> = {
  pending: '排队中',
  running: '执行中',
  ok: '已完成',
  skipped: '已跳过',
  failed: '失败',
  ask_user_paused: '等待用户',
};

const DOT_CLASS: Record<OrchestrationTaskStatus, string> = {
  pending: 'bg-[var(--agent-muted-foreground,#9ca3af)]',
  running: 'bg-amber-500 animate-pulse',
  ok: 'bg-emerald-500',
  skipped: 'bg-[var(--agent-muted-foreground,#d1d5db)]',
  failed: 'bg-rose-500',
  ask_user_paused: 'bg-sky-500',
};

export const OrchestrationPlanCard: React.FC<OrchestrationPlanCardProps> = ({
  payload,
  taskStatuses,
  agentNameFor,
  className,
  defaultExpanded = true,
  expanded: expandedProp,
  onExpandedChange,
  loading,
  loadingHint,
  failed,
  failure,
  headerLabel,
  headerSecondary,
  hideMode,
  hideHeaderCounts,
  renderAbove,
  renderBelowHeader,
  renderTaskRow,
}) => {
  const [expandedState, setExpandedState] = React.useState(defaultExpanded);
  const isControlled = expandedProp !== undefined;
  const expanded = isControlled ? Boolean(expandedProp) : expandedState;
  const setExpanded = (next: boolean) => {
    if (!isControlled) setExpandedState(next);
    onExpandedChange?.(next);
  };

  const above = renderAbove ? asReactNode(renderAbove()) : null;
  const belowHeader = renderBelowHeader ? asReactNode(renderBelowHeader()) : null;

  // ---------------- loading shell ----------------
  if (loading) {
    return (
      <div
        className={[
          'steerable-orchestration-plan',
          className,
        ].filter(Boolean).join(' ')}
      >
        {above}
        <div className="rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm shadow-sm">
          <div className="flex w-full items-center gap-2 px-3 py-2 text-xs text-[var(--agent-muted-foreground,#6b7280)]">
            <LoaderIcon size={14} className="shrink-0 animate-spin" />
            <GitBranchIcon size={14} className="shrink-0" />
            <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
              {headerLabel ?? (
                <span className="font-medium text-[var(--agent-foreground,#111827)]">协调员</span>
              )}
              <span className="text-[var(--agent-muted-foreground,#9ca3af)]">·</span>
              <span>{loadingHint ?? '正在规划'}</span>
            </span>
          </div>
          {belowHeader}
        </div>
      </div>
    );
  }

  // ---------------- failed shell ----------------
  if (failed) {
    return (
      <div
        className={[
          'steerable-orchestration-plan',
          className,
        ].filter(Boolean).join(' ')}
      >
        {above}
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 text-sm shadow-sm">
          <div className="flex w-full items-start gap-2 px-3 py-2 text-xs">
            <AlertIcon size={14} className="mt-0.5 shrink-0 text-rose-500" />
            <GitBranchIcon size={14} className="mt-0.5 shrink-0 text-[var(--agent-muted-foreground,#6b7280)]" />
            <div className="min-w-0 flex-1">
              <div className="inline-flex items-center gap-1.5 whitespace-nowrap">
                {headerLabel ?? (
                  <span className="font-medium text-[var(--agent-foreground,#111827)]">协调员</span>
                )}
                <span className="text-[var(--agent-muted-foreground,#9ca3af)]">·</span>
                <span className="text-rose-500">规划失败</span>
              </div>
              {failure && (
                <div className="mt-1 break-words leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]">
                  {failure}
                </div>
              )}
            </div>
          </div>
          {belowHeader}
        </div>
      </div>
    );
  }

  // ---------------- default ready shell ----------------
  const tasks = payload.tasks ?? [];

  const counts = React.useMemo(() => {
    const c = { running: 0, ok: 0, failed: 0, paused: 0, total: tasks.length };
    for (const t of tasks) {
      const s = (taskStatuses?.[t.id] ?? 'pending') as OrchestrationTaskStatus;
      if (s === 'running') c.running += 1;
      else if (s === 'ok') c.ok += 1;
      else if (s === 'failed') c.failed += 1;
      else if (s === 'ask_user_paused') c.paused += 1;
    }
    return c;
  }, [tasks, taskStatuses]);

  return (
    <div
      className={[
        'steerable-orchestration-plan',
        className,
      ].filter(Boolean).join(' ')}
    >
      {above}
      <div className="rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm shadow-sm">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs text-[var(--agent-muted-foreground,#6b7280)] transition-all duration-200 hover:text-[var(--agent-foreground,#111827)]"
        >
          <span className="flex min-w-0 items-center gap-2">
            {expanded ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
            <GitBranchIcon size={14} className="shrink-0" />
            <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap">
              {headerLabel ?? (
                <span className="font-medium text-[var(--agent-foreground,#111827)]">编排计划</span>
              )}
              {headerSecondary !== undefined ? (
                <>
                  <span className="text-[var(--agent-muted-foreground,#9ca3af)]">·</span>
                  <span>{headerSecondary}</span>
                </>
              ) : (
                <>
                  <span className="text-[var(--agent-muted-foreground,#9ca3af)]">·</span>
                  <span>{tasks.length} 个子任务</span>
                </>
              )}
              {!hideMode && payload.mode && (
                <span className="ml-1 rounded-full bg-[var(--agent-muted,#f3f4f6)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--agent-muted-foreground,#6b7280)]">
                  {payload.mode}
                </span>
              )}
            </span>
            {payload.rationale && !expanded && (
              <span className="min-w-0 flex-1 truncate text-[var(--agent-muted-foreground,#9ca3af)]">
                {payload.rationale}
              </span>
            )}
          </span>
          {!hideHeaderCounts && (
            <span className="flex shrink-0 items-center gap-2">
              {counts.running > 0 && (
                <span className="flex items-center gap-1">
                  <LoaderIcon size={12} className="text-amber-500" /> {counts.running}
                </span>
              )}
              {counts.failed > 0 && (
                <span className="flex items-center gap-1 text-rose-600">
                  <AlertIcon size={12} /> {counts.failed}
                </span>
              )}
              <span>
                {counts.ok}/{counts.total}
              </span>
            </span>
          )}
        </button>

        {belowHeader}

        {expanded && (
          <div className="px-3 pb-3">
            {payload.rationale && (
              <p className="pb-2 text-xs leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]">
                {payload.rationale}
              </p>
            )}
            <ul className="space-y-1.5">
              {tasks.map((t) => {
                const status = (taskStatuses?.[t.id] ?? 'pending') as OrchestrationTaskStatus;
                if (renderTaskRow) {
                  return (
                    <React.Fragment key={t.id}>
                      {asReactNode(renderTaskRow(t, status))}
                    </React.Fragment>
                  );
                }
                return (
                  <li key={t.id} className="flex items-start gap-2 text-sm">
                    <span
                      className={`mt-1.5 h-2 w-2 flex-shrink-0 rounded-full ${DOT_CLASS[status]}`}
                      aria-hidden
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-[var(--agent-foreground,#111827)]">
                          {agentNameFor ? agentNameFor(t.agentId) : t.agentId}
                        </span>
                        <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                          {STATUS_LABEL[status]}
                        </span>
                      </div>
                      {t.prompt && (
                        <p className="mt-0.5 line-clamp-2 text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                          {t.prompt}
                        </p>
                      )}
                      {(t.dependsOn?.length ?? 0) > 0 && (
                        <p className="mt-0.5 text-[10px] uppercase tracking-wider text-[var(--agent-muted-foreground,#9ca3af)]">
                          依赖：{t.dependsOn!.join(', ')}
                        </p>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
};

export { STATUS_LABEL as ORCHESTRATION_STATUS_LABEL, DOT_CLASS as ORCHESTRATION_DOT_CLASS };

export default OrchestrationPlanCard;
