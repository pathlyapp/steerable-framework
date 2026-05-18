/**
 * `<ResearchPlanCard />`
 *
 * Snapshot of a research agent's current sub-question tree, with per-question
 * status badges (searching / strong evidence / conflicted / ...) and the
 * coordinator's decision for the next round.
 *
 * Features:
 *  - progress bar (round / totalRounds);
 *  - sub-question note rendering when `note` is present;
 *  - boxed "next decision" footer with the agent's reasoning.
 */
import * as React from 'react';
import type { ResearchPlanPayload } from '@steerable/agent-protocol';
import { GitBranchIcon, ListChecksIcon } from './icons.js';

type Kind = 'fact' | 'compare' | 'conclusion' | 'risk';
type Status =
  | 'pending'
  | 'searching'
  | 'evidenced_strong'
  | 'evidenced_medium'
  | 'evidenced_weak'
  | 'conflicted'
  | 'exhausted';

const KIND_LABELS: Record<Kind, string> = {
  fact: '事实',
  compare: '对比',
  conclusion: '结论',
  risk: '风险',
};

const STATUS_LABELS: Record<Status, string> = {
  pending: '待开始',
  searching: '检索中',
  evidenced_strong: '强证据',
  evidenced_medium: '中证据',
  evidenced_weak: '弱证据',
  conflicted: '证据冲突',
  exhausted: '已穷尽',
};

const STATUS_COLOR: Record<Status, string> = {
  pending: 'bg-[var(--agent-muted,#f3f4f6)] text-[var(--agent-muted-foreground,#6b7280)]',
  searching: 'bg-[var(--agent-muted,#f3f4f6)] text-[var(--agent-muted-foreground,#6b7280)]',
  evidenced_strong: 'bg-green-500/15 text-green-700 dark:text-green-400',
  evidenced_medium: 'bg-blue-500/15 text-blue-700 dark:text-blue-400',
  evidenced_weak: 'bg-amber-500/15 text-amber-700 dark:text-amber-400',
  conflicted: 'bg-red-500/15 text-red-700 dark:text-red-400',
  exhausted: 'bg-zinc-500/15 text-zinc-700 dark:text-zinc-400',
};

const DECISION_LABEL: Record<'continue' | 'expand' | 'converge', string> = {
  continue: '继续深挖',
  expand: '横向扩展',
  converge: '进入收敛',
};

export interface ResearchPlanCardProps {
  payload: ResearchPlanPayload;
  /** Total expected rounds (used to compute the progress bar). Defaults to 3. */
  totalRounds?: number;
  className?: string;
}

export const ResearchPlanCard: React.FC<ResearchPlanCardProps> = ({
  payload,
  totalRounds = 3,
  className,
}) => {
  const subQuestions = payload.subQuestions ?? [];
  const round = Math.max(0, Math.min(totalRounds, payload.round ?? 0));
  const progressPercent = totalRounds > 0
    ? Math.max(0, Math.min(100, (round / totalRounds) * 100))
    : 0;
  const decisionLabel =
    payload.decision &&
    DECISION_LABEL[payload.decision.next as 'continue' | 'expand' | 'converge'];

  return (
    <div
      className={[
        'steerable-research-plan rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="space-y-3 p-3">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <GitBranchIcon size={14} className="shrink-0 text-blue-500" />
              <span className="truncate text-sm font-medium">{payload.topic}</span>
            </div>
            <div className="inline-flex shrink-0 items-center gap-1">
              <span className="rounded-full bg-[var(--agent-muted,#f3f4f6)] px-2 py-0.5 text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                第 {round}/{totalRounds} 轮
              </span>
              {payload.final && (
                <span className="rounded-full bg-green-500/15 px-2 py-0.5 text-xs text-green-700 dark:text-green-400">
                  已收敛
                </span>
              )}
            </div>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-[var(--agent-muted,#f3f4f6)]">
            <div
              className="h-full bg-blue-500 transition-all duration-200"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <ListChecksIcon size={14} className="text-blue-500" />
            子问题状态
          </div>
          <div className="space-y-2">
            {subQuestions.map((q) => {
              const kind = (q.kind ?? 'fact') as Kind;
              const status = (q.status ?? 'pending') as Status;
              return (
                <div
                  key={q.id}
                  className="rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] p-2.5"
                >
                  <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                    <span className="rounded-full bg-[var(--agent-muted,#f3f4f6)] px-1.5 py-0.5 text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                      {KIND_LABELS[kind] ?? kind}
                    </span>
                    <span className={`rounded-full px-1.5 py-0.5 text-xs ${STATUS_COLOR[status]}`}>
                      {STATUS_LABELS[status] ?? status}
                    </span>
                    <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                      证据 {q.evidenceCount ?? 0}
                    </span>
                  </div>
                  <div className="text-sm">{q.question}</div>
                  {q.note && (
                    <div className="mt-1 whitespace-pre-wrap text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                      {q.note}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {payload.decision && (
          <div className="rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/40 px-2.5 py-2">
            <div className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">下一步决策</div>
            <div className="text-sm font-medium">
              {decisionLabel ?? payload.decision.next}
            </div>
            {payload.decision.reason && (
              <div className="mt-0.5 whitespace-pre-wrap text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                {payload.decision.reason}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default ResearchPlanCard;
