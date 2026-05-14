/**
 * `OrchestrationPlanCard` — render a multi-step plan with per-step status.
 *
 * Maps to the `orchestration` SSE event family: the harness emits a structured
 * plan up-front and updates step status as execution progresses. The card is
 * intentionally read-only; consumers that want inline edit (`deeppath/apps/web`'s
 * "PlanSteps") should compose their own editor using `useChatStream`'s
 * `onUnknownEvent` hook to track plan events and feed updated steps in here.
 */

import { useMemo } from 'react';
import { cn } from './cn.js';

export type OrchestrationStepStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'error'
  | 'skipped';

export interface OrchestrationStep {
  id: string;
  title: string;
  description?: string;
  status: OrchestrationStepStatus;
  /** Optional badge (`tool`, `wait`, `decision`, `agent:<name>`, …). */
  kind?: string;
  /** Optional ISO datetime when the step finished, used for the right column. */
  finishedAt?: string;
}

export interface OrchestrationPlanCardProps {
  title?: string;
  steps: OrchestrationStep[];
  className?: string;
  /**
   * Optional click handler — useful when you want to open a step's trace
   * timeline in a side panel. Receives the step that was clicked.
   */
  onStepClick?: (step: OrchestrationStep) => void;
}

const statusToIcon: Record<OrchestrationStepStatus, string> = {
  pending: '○',
  running: '◐',
  done: '●',
  error: '✕',
  skipped: '—',
};

const statusToColor: Record<OrchestrationStepStatus, string> = {
  pending: 'text-agent-muted-foreground',
  running: 'text-agent-accent',
  done: 'text-agent-tool-read',
  error: 'text-agent-destructive',
  skipped: 'text-agent-muted-foreground line-through',
};

export function OrchestrationPlanCard(props: OrchestrationPlanCardProps) {
  const { title = 'Plan', steps, className, onStepClick } = props;

  const summary = useMemo(() => {
    const total = steps.length;
    const done = steps.filter((s) => s.status === 'done').length;
    const errored = steps.filter((s) => s.status === 'error').length;
    return { total, done, errored };
  }, [steps]);

  return (
    <div
      className={cn(
        'rounded-agent-lg border border-agent-border bg-agent-canvas p-4',
        className,
      )}
      data-testid="orchestration-plan-card"
    >
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-agent-foreground">
            {title}
          </span>
          <span className="rounded-full border border-agent-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-agent-muted-foreground">
            {summary.done}/{summary.total}
          </span>
        </div>
        {summary.errored > 0 ? (
          <span className="text-[11px] font-medium text-agent-destructive">
            {summary.errored} error{summary.errored === 1 ? '' : 's'}
          </span>
        ) : null}
      </div>
      <ol className="space-y-1.5">
        {steps.map((step, idx) => {
          const interactive = Boolean(onStepClick);
          const Tag: 'button' | 'div' = interactive ? 'button' : 'div';
          return (
            <li key={step.id}>
              <Tag
                {...(interactive
                  ? {
                      type: 'button' as const,
                      onClick: () => onStepClick?.(step),
                    }
                  : {})}
                className={cn(
                  'flex w-full items-start gap-2 rounded-agent-sm px-2 py-1.5 text-left',
                  interactive
                    ? 'transition-colors hover:bg-agent-muted'
                    : '',
                )}
                data-status={step.status}
              >
                <span
                  className={cn(
                    'mt-0.5 select-none font-mono text-base leading-none',
                    statusToColor[step.status],
                  )}
                  aria-hidden
                >
                  {statusToIcon[step.status]}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-xs font-mono text-agent-muted-foreground">
                      {(idx + 1).toString().padStart(2, '0')}
                    </span>
                    <span
                      className={cn(
                        'truncate text-sm font-medium',
                        statusToColor[step.status],
                      )}
                    >
                      {step.title}
                    </span>
                    {step.kind ? (
                      <span className="shrink-0 rounded-full border border-agent-border bg-agent-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-agent-muted-foreground">
                        {step.kind}
                      </span>
                    ) : null}
                  </div>
                  {step.description ? (
                    <p className="mt-0.5 text-xs text-agent-muted-foreground">
                      {step.description}
                    </p>
                  ) : null}
                </div>
                {step.finishedAt ? (
                  <span className="shrink-0 text-[10px] text-agent-muted-foreground">
                    {step.finishedAt}
                  </span>
                ) : null}
              </Tag>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
