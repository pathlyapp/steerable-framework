/**
 * `<PlanSelectorCard />`
 *
 * Two-to-four candidate plans with effort / risk metrics and pros/cons.
 *
 * Calls `onSelect(planId)` once the user picks one. If `payload.selectedPlan`
 * is filled in by the backend the card switches to read-only mode and
 * highlights the chosen plan.
 *
 * Behaviour switches:
 *  - `expandableDetails` (default `false`): when on, only the plan summary +
 *    metrics + select button are visible by default; approach / pros / cons
 *    sit behind a "查看详情" toggle.
 *
 * Slots:
 *  - `renderHeader(plansCount)` -- replace the "候选方案 / 为你准备了 N 个方案"
 *    label area.
 *  - `renderBottomHint({ submitted })` -- swap the hint shown below the list.
 *  - `existingGoalPrefix`, `newGoalPrefix` -- localise goal-attribution
 *    phrasing.
 */
import * as React from 'react';
import type { PlanSelectorPayload } from '@steerable/agent-protocol';
import {
  ClockIcon,
  FlameIcon,
  ShieldIcon,
  CheckIcon,
  TargetIcon,
  AlertIcon,
  ThumbsUpIcon,
  LoaderIcon,
  ChevronDownIcon,
  ChevronUpIcon,
} from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

type Level = 'low' | 'medium' | 'high';

const EFFORT_CONFIG: Record<Level, { label: string; text: string; bg: string }> = {
  low: {
    label: '低投入',
    text: 'text-green-600 dark:text-green-400',
    bg: 'bg-green-50 dark:bg-green-950/30',
  },
  medium: {
    label: '中投入',
    text: 'text-amber-600 dark:text-amber-400',
    bg: 'bg-amber-50 dark:bg-amber-950/30',
  },
  high: {
    label: '高投入',
    text: 'text-red-600 dark:text-red-400',
    bg: 'bg-red-50 dark:bg-red-950/30',
  },
};
const RISK_CONFIG: Record<Level, { label: string; text: string; bg: string }> = {
  low: {
    label: '低风险',
    text: 'text-green-600 dark:text-green-400',
    bg: 'bg-green-50 dark:bg-green-950/30',
  },
  medium: {
    label: '中风险',
    text: 'text-amber-600 dark:text-amber-400',
    bg: 'bg-amber-50 dark:bg-amber-950/30',
  },
  high: {
    label: '高风险',
    text: 'text-red-600 dark:text-red-400',
    bg: 'bg-red-50 dark:bg-red-950/30',
  },
};

export interface PlanSelectorCardProps {
  payload: PlanSelectorPayload;
  isComplete?: boolean;
  onSelect?: (planId: string) => void;
  className?: string;
  expandableDetails?: boolean;
  existingGoalPrefix?: string;
  newGoalPrefix?: string;
  renderHeader?: (plansCount: number) => RenderSlotResult;
  renderBottomHint?: (args: { submitted: boolean }) => RenderSlotResult;
}

export const PlanSelectorCard: React.FC<PlanSelectorCardProps> = ({
  payload,
  isComplete,
  onSelect,
  className,
  expandableDetails = false,
  existingGoalPrefix = '归属目标：',
  newGoalPrefix = '新目标：',
  renderHeader,
  renderBottomHint,
}) => {
  const isReadOnly = Boolean(isComplete || payload.selectedPlan);
  const plans = payload.plans ?? [];
  const [submittingFor, setSubmittingFor] = React.useState<string | null>(null);
  const [expandedId, setExpandedId] = React.useState<string | null>(null);

  const goalLabel = (() => {
    const g = payload.goalAttribution;
    if (!g) return null;
    if (g.type === 'existing') return `${existingGoalPrefix}${g.existingGoalTitle ?? ''}`;
    return `${newGoalPrefix}${g.newGoalTitle ?? ''}`;
  })();

  const handleSelect = (planId: string) => {
    if (!onSelect || isReadOnly) return;
    setSubmittingFor(planId);
    try {
      onSelect(planId);
    } finally {
      setSubmittingFor(null);
    }
  };

  return (
    <div
      className={[
        'steerable-plan-selector rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="space-y-3 p-3">
        {renderHeader ? (
          <div>{asReactNode(renderHeader(plans.length))}</div>
        ) : (
          <div className="flex items-center gap-2">
            <TargetIcon size={14} className="text-blue-500" />
            <span className="text-sm font-medium">候选方案</span>
            {goalLabel && (
              <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                · {goalLabel}
              </span>
            )}
          </div>
        )}
        {renderHeader && goalLabel && (
          <p className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">{goalLabel}</p>
        )}

        <div className="space-y-2">
          {plans.map((p) => {
            const isSelected = payload.selectedPlan === p.id;
            const effort = EFFORT_CONFIG[p.metrics?.effortLevel || 'medium'];
            const risk = RISK_CONFIG[p.metrics?.riskLevel || 'medium'];
            const isExpanded = expandableDetails ? expandedId === p.id : true;

            return (
              <div
                key={p.id}
                className={[
                  'rounded-lg border transition-all duration-200',
                  isSelected
                    ? 'border-blue-400 dark:border-blue-600 bg-blue-50/50 dark:bg-blue-950/20 ring-1 ring-blue-200 dark:ring-blue-800'
                    : 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] hover:border-[var(--agent-border,#e5e7eb)]/80',
                  isReadOnly && !isSelected ? 'opacity-50' : '',
                ].filter(Boolean).join(' ')}
              >
                <div className="p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <h4 className="truncate text-sm font-medium">{p.name}</h4>
                        {isSelected && (
                          <span className="inline-flex items-center gap-0.5 rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
                            <CheckIcon size={10} /> 已选择
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]">
                        {p.summary}
                      </p>
                    </div>
                  </div>

                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    {p.metrics?.duration && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-[var(--agent-muted,#f3f4f6)] px-2 py-0.5 text-[10px] font-medium text-[var(--agent-muted-foreground,#6b7280)]">
                        <ClockIcon size={11} />
                        {p.metrics.duration}
                      </span>
                    )}
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${effort.bg} ${effort.text}`}
                    >
                      <FlameIcon size={11} />
                      {effort.label}
                    </span>
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${risk.bg} ${risk.text}`}
                    >
                      <ShieldIcon size={11} />
                      {risk.label}
                    </span>
                  </div>

                  <div className="mt-2 flex items-center justify-between">
                    {expandableDetails ? (
                      <button
                        type="button"
                        onClick={() => setExpandedId(isExpanded ? null : p.id)}
                        className="inline-flex items-center gap-1 text-[11px] text-[var(--agent-muted-foreground,#6b7280)] transition-colors duration-200 hover:text-[var(--agent-foreground,#111827)]"
                      >
                        {isExpanded ? (
                          <>
                            <ChevronUpIcon size={11} /> 收起详情
                          </>
                        ) : (
                          <>
                            <ChevronDownIcon size={11} /> 查看详情
                          </>
                        )}
                      </button>
                    ) : (
                      <span />
                    )}

                    {!isReadOnly && onSelect && (
                      <button
                        type="button"
                        onClick={() => handleSelect(p.id)}
                        disabled={submittingFor !== null}
                        className="inline-flex items-center gap-1 rounded-full bg-[var(--agent-primary,#18181b)] px-3 py-1 text-xs font-medium text-[var(--agent-primary-foreground,#fff)] shadow-sm transition-all duration-200 hover:opacity-90 disabled:opacity-50"
                      >
                        {submittingFor === p.id && <LoaderIcon size={10} />}
                        选择此方案
                      </button>
                    )}
                  </div>
                </div>

                {isExpanded && (
                  <div className="space-y-2 border-t border-[var(--agent-border,#e5e7eb)]/50 px-3 pb-3 pt-2">
                    {p.approach && (
                      <p className="text-xs leading-relaxed">{p.approach}</p>
                    )}
                    <div className="grid grid-cols-2 gap-2">
                      {(p.pros?.length ?? 0) > 0 && (
                        <div className="space-y-1">
                          <div className="flex items-center gap-1 text-[10px] font-medium text-green-600 dark:text-green-400">
                            <ThumbsUpIcon size={11} />
                            优势
                          </div>
                          {p.pros!.map((pro, i) => (
                            <p
                              key={`pro-${i}`}
                              className="pl-4 text-[11px] text-[var(--agent-muted-foreground,#6b7280)]"
                            >
                              {pro}
                            </p>
                          ))}
                        </div>
                      )}
                      {(p.cons?.length ?? 0) > 0 && (
                        <div className="space-y-1">
                          <div className="flex items-center gap-1 text-[10px] font-medium text-amber-600 dark:text-amber-400">
                            <AlertIcon size={11} />
                            注意
                          </div>
                          {p.cons!.map((con, i) => (
                            <p
                              key={`con-${i}`}
                              className="pl-4 text-[11px] text-[var(--agent-muted-foreground,#6b7280)]"
                            >
                              {con}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                    {p.bestFor && (
                      <p className="text-[11px] italic text-[var(--agent-muted-foreground,#6b7280)]">
                        适合：{p.bestFor}
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {payload.comparison && (
          <p className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
            {payload.comparison}
          </p>
        )}

        <div className="flex items-center justify-between pt-1">
          {renderBottomHint
            ? asReactNode(renderBottomHint({ submitted: isReadOnly }))
            : !isReadOnly
              ? <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                  点击选择方案
                </span>
              : <div className="flex items-center gap-1.5">
                  <CheckIcon size={12} className="text-blue-500" />
                  <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                    方案已选择
                  </span>
                </div>}
        </div>
      </div>
    </div>
  );
};

export default PlanSelectorCard;
