/**
 * `<CoverageReportCard />`
 *
 * Renders mastery / coverage of a knowledge graph: overall numbers, section
 * progress bars, and a list of weak knowledge points with recommendations.
 * Offers a "practice weak points" action (optionally with multi-select) when
 * `payload.actions.allowRemediateQuiz` is set.
 *
 * Configurable bits:
 *  - `progressMetric` ('mastery' default, or 'coverage') controls which value
 *    fills the per-section progress bar.
 *  - `allowWeakPointSelection` turns the weak-points list into checkboxes;
 *    `onRemediate` then receives the explicit selected ids (or all ids if
 *    nothing is checked).
 *  - `renderSectionStats(section)` slot lets consumers attach an info line
 *    under each progress bar (e.g. "已学 5/12 · 已测 3 · 已掌握 2").
 *  - `remediateDisabled` + `remediateLabel` + `remediateHint` let the host
 *    surface submission state (e.g. "已提交补救请求").
 *  - `emptyWeakPointsLabel` shows a positive empty state when there are no
 *    weak points to display.
 */
import * as React from 'react';
import type { CoverageReportPayload } from '@steerable/agent-protocol';
import { ChartIcon, CheckIcon, AlertIcon } from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

type Section = NonNullable<CoverageReportPayload['sections']>[number];

export interface CoverageReportCardProps {
  payload: CoverageReportPayload;
  isComplete?: boolean;
  onRemediate?: (selectedIds: string[]) => void;
  className?: string;
  progressMetric?: 'coverage' | 'mastery';
  allowWeakPointSelection?: boolean;
  remediateDisabled?: boolean;
  remediateLabel?: string;
  remediateHint?: React.ReactNode;
  emptyWeakPointsLabel?: React.ReactNode;
  renderSectionStats?: (section: Section) => RenderSlotResult;
}

function pct(v: number | null | undefined): string {
  const safe = Number.isFinite(v ?? NaN) ? (v as number) : 0;
  const c = Math.max(0, Math.min(1, safe));
  return `${Math.round(c * 100)}%`;
}

export const CoverageReportCard: React.FC<CoverageReportCardProps> = ({
  payload,
  isComplete,
  onRemediate,
  className,
  progressMetric = 'mastery',
  allowWeakPointSelection = false,
  remediateDisabled = false,
  remediateLabel,
  remediateHint,
  emptyWeakPointsLabel,
  renderSectionStats,
}) => {
  const sections = payload.sections ?? [];
  const weakPoints = payload.weakPoints ?? [];

  const [selected, setSelected] = React.useState<string[]>([]);
  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const handleClick = () => {
    if (!onRemediate || remediateDisabled) return;
    const ids =
      allowWeakPointSelection && selected.length > 0
        ? selected
        : weakPoints.map((wp) => wp.id);
    onRemediate(ids);
  };

  return (
    <div
      className={[
        'steerable-coverage-report rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="space-y-3 p-3">
        <div className="flex items-center gap-2">
          <ChartIcon size={14} className="text-blue-500" />
          <span className="font-medium">{payload.title}</span>
          {isComplete && <CheckIcon size={12} className="text-emerald-600" />}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] p-2">
            <p className="text-[11px] text-[var(--agent-muted-foreground,#6b7280)]">整体覆盖率</p>
            <p className="text-base font-semibold">{pct(payload.overallCoverage)}</p>
          </div>
          <div className="rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] p-2">
            <p className="text-[11px] text-[var(--agent-muted-foreground,#6b7280)]">整体掌握率</p>
            <p className="text-base font-semibold">{pct(payload.overallMastery)}</p>
          </div>
        </div>

        {payload.summary && (
          <p className="text-xs text-[var(--agent-muted-foreground,#6b7280)] whitespace-pre-line">
            {payload.summary}
          </p>
        )}

        <div className="space-y-2">
          {sections.map((section) => {
            const fill = progressMetric === 'coverage' ? section.coverage : section.mastery;
            return (
              <div
                key={section.id}
                className="rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] p-2"
              >
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-sm font-medium">{section.name}</span>
                  <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                    覆盖 {pct(section.coverage)} · 掌握 {pct(section.mastery)}
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--agent-muted,#f3f4f6)]">
                  <div
                    className={
                      progressMetric === 'coverage'
                        ? 'h-full bg-[var(--agent-primary,#18181b)] transition-all duration-200'
                        : 'h-full bg-emerald-500 transition-all duration-200'
                    }
                    style={{ width: pct(fill) }}
                    aria-hidden
                  />
                </div>
                {renderSectionStats && (
                  <div className="mt-1 text-[11px] text-[var(--agent-muted-foreground,#6b7280)]">
                    {asReactNode(renderSectionStats(section))}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div className="space-y-2 rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] p-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-amber-600 dark:text-amber-400">
            <AlertIcon size={12} />
            薄弱知识点
          </div>
          {weakPoints.length === 0 ? (
            emptyWeakPointsLabel ? (
              <div className="inline-flex items-center gap-1.5 text-xs text-green-600 dark:text-green-400">
                <CheckIcon size={12} />
                {emptyWeakPointsLabel}
              </div>
            ) : null
          ) : allowWeakPointSelection ? (
            <div className="space-y-1.5">
              {weakPoints.map((point) => (
                <label key={point.id} className="flex cursor-pointer items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5 h-4 w-4"
                    checked={selected.includes(point.id)}
                    onChange={() => toggle(point.id)}
                    disabled={remediateDisabled}
                  />
                  <div className="min-w-0 text-xs">
                    <p className="font-medium">
                      {point.name}
                      {point.sectionName ? ` · ${point.sectionName}` : ''}
                    </p>
                    <p className="text-[var(--agent-muted-foreground,#6b7280)]">
                      正确率 {pct(point.accuracy)} · {point.recommendation}
                    </p>
                  </div>
                </label>
              ))}
            </div>
          ) : (
            <ul className="space-y-1 text-xs">
              {weakPoints.map((wp) => (
                <li key={wp.id}>
                  <span className="font-medium">{wp.name}</span>
                  {wp.sectionName && (
                    <span className="ml-1 text-[var(--agent-muted-foreground,#6b7280)]">· {wp.sectionName}</span>
                  )}
                  <span className="ml-1 text-[var(--agent-muted-foreground,#6b7280)]">· 正确率 {pct(wp.accuracy)}</span>
                  <p className="text-[var(--agent-muted-foreground,#6b7280)]">{wp.recommendation}</p>
                </li>
              ))}
            </ul>
          )}
        </div>

        {payload.actions?.allowRemediateQuiz && onRemediate && weakPoints.length > 0 && (
          <div className="flex items-center justify-between pt-1">
            <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
              {remediateHint}
            </span>
            <button
              type="button"
              onClick={handleClick}
              disabled={remediateDisabled}
              className="inline-flex h-[28px] items-center rounded-full bg-[var(--agent-primary,#18181b)] px-3 text-xs font-medium text-[var(--agent-primary-foreground,#fff)] transition-all duration-200 hover:opacity-90 disabled:opacity-50"
            >
              {remediateLabel || payload.actions.remediateActionLabel || '为薄弱点出题'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default CoverageReportCard;
