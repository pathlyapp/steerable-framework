/**
 * `<PlanStepsCard />`
 *
 * Compact horizontal checklist of skill names: `step1 / step2 / step3`. Used
 * to show what the agent intends to do this turn. Pure render off
 * `PlanStepsPayload`; the parent passes `isStreaming` if it wants the pulsing
 * cursor dot at the end.
 */
import * as React from 'react';
import type { PlanStepsPayload } from '@steerable/agent-protocol';
import { ListChecksIcon } from './icons.js';

export interface PlanStepsCardProps {
  payload: PlanStepsPayload;
  isStreaming?: boolean;
  className?: string;
}

export const PlanStepsCard: React.FC<PlanStepsCardProps> = ({ payload, isStreaming, className }) => {
  const steps = payload.steps ?? [];
  if (steps.length === 0 && !isStreaming) return null;

  return (
    <div
      className={[
        'steerable-plan-steps flex items-start gap-2 rounded-lg border px-3 py-2 transition-all duration-200',
        isStreaming
          ? 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#f9fafb)] shadow-sm'
          : 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <ListChecksIcon size={14} className="mt-0.5 flex-shrink-0 text-[var(--agent-muted-foreground,#6b7280)]" />
      <div className="flex flex-wrap items-center gap-1 text-xs text-[var(--agent-muted-foreground,#6b7280)]">
        {steps.map((step, idx) => (
          <React.Fragment key={`${idx}-${step}`}>
            {idx > 0 && <span aria-hidden className="select-none opacity-40">/</span>}
            <span className="whitespace-nowrap">{step}</span>
          </React.Fragment>
        ))}
        {isStreaming && (
          <span
            aria-hidden
            className="ml-1 inline-block h-1.5 w-1.5 translate-y-[1px] animate-pulse rounded-full bg-current"
          />
        )}
      </div>
    </div>
  );
};

export default PlanStepsCard;
