/**
 * `<SummaryMessageCard />`
 *
 * Compact summary of N previous messages (e.g. history compaction). Collapsed
 * by default so it doesn't dominate the chat scrollback.
 *
 * Appearance variants:
 *  - `card` (default): bordered chrome.
 *  - `inline`: a small pill button + indented body block below. Matches the
 *    deeppath "above the input" aesthetic.
 *
 * Slots:
 *  - `renderMarkdown(body)` -- supply your own markdown renderer.
 *  - `renderLabel({ isPending, count })` -- replace the trigger label.
 *  - `renderToggle({ expanded, canToggle })` -- replace the toggle chevron + text.
 */
import * as React from 'react';
import type { SummaryMessagePayload } from '@steerable/agent-protocol';
import { ArchiveIcon, ChevronDownIcon, ChevronRightIcon, LoaderIcon } from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

export type SummaryMessageCardAppearance = 'card' | 'inline';

export interface SummaryMessageLabelArgs {
  isPending: boolean;
  count: number | null;
}

export interface SummaryMessageToggleArgs {
  expanded: boolean;
  canToggle: boolean;
}

export interface SummaryMessageCardProps {
  payload: SummaryMessagePayload;
  renderMarkdown?: (body: string) => RenderSlotResult;
  renderLabel?: (args: SummaryMessageLabelArgs) => RenderSlotResult;
  renderToggle?: (args: SummaryMessageToggleArgs) => RenderSlotResult;
  appearance?: SummaryMessageCardAppearance;
  className?: string;
  defaultExpanded?: boolean;
}

export const SummaryMessageCard: React.FC<SummaryMessageCardProps> = ({
  payload,
  renderMarkdown,
  renderLabel,
  renderToggle,
  appearance = 'card',
  className,
  defaultExpanded = false,
}) => {
  const [expanded, setExpanded] = React.useState(defaultExpanded);
  const isPending = payload.status === 'pending';
  const count =
    typeof payload.summarizedCount === 'number' && payload.summarizedCount > 0
      ? payload.summarizedCount
      : null;
  const hasBody = Boolean(payload.body && payload.body.trim());
  const canToggle = !isPending && hasBody;

  const defaultLabel = isPending
    ? '总结中…'
    : `历史摘要${count ? `（${count} 条消息）` : ''}`;
  const labelNode: React.ReactNode = renderLabel
    ? asReactNode(renderLabel({ isPending, count }))
    : defaultLabel;

  const defaultToggle = expanded
    ? <ChevronDownIcon size={12} />
    : <ChevronRightIcon size={12} />;
  const toggleNode: React.ReactNode = renderToggle
    ? asReactNode(renderToggle({ expanded, canToggle }))
    : defaultToggle;

  const body: React.ReactNode = expanded && canToggle ? (
    renderMarkdown ? (
      asReactNode(renderMarkdown(payload.body))
    ) : (
      <pre className="whitespace-pre-wrap break-words font-sans">{payload.body}</pre>
    )
  ) : null;

  if (appearance === 'inline') {
    return (
      <div className={['steerable-summary-message', className].filter(Boolean).join(' ')}>
        <button
          type="button"
          onClick={() => canToggle && setExpanded((v) => !v)}
          disabled={!canToggle}
          className={[
            'group inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] leading-none text-[var(--agent-muted-foreground,#6b7280)] transition-colors',
            canToggle
              ? 'cursor-pointer hover:bg-[var(--agent-muted,#f3f4f6)] hover:text-[var(--agent-foreground,#111827)]'
              : 'cursor-default',
          ].join(' ')}
        >
          {isPending ? (
            <LoaderIcon size={12} className="animate-spin" />
          ) : (
            <ArchiveIcon size={12} />
          )}
          <span>{labelNode}</span>
          {canToggle && (
            <span className="inline-flex items-center gap-0.5 opacity-60 group-hover:opacity-100">
              {toggleNode}
            </span>
          )}
        </button>
        {body && (
          <div className="ml-3 mt-1.5 rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/30 px-3 py-2 text-[12px] leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]">
            {body}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      className={[
        'steerable-summary-message rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/40 text-xs',
        className,
      ].filter(Boolean).join(' ')}
    >
      <button
        type="button"
        onClick={() => canToggle && setExpanded((v) => !v)}
        disabled={!canToggle}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[var(--agent-muted-foreground,#6b7280)]"
      >
        {isPending ? <LoaderIcon size={12} /> : <ArchiveIcon size={12} />}
        <span>{labelNode}</span>
        {canToggle && <span className="ml-auto">{toggleNode}</span>}
      </button>
      {body && (
        <div className="border-t border-[var(--agent-border,#e5e7eb)] px-3 py-2 text-[var(--agent-foreground,#111827)]">
          {body}
        </div>
      )}
    </div>
  );
};

export default SummaryMessageCard;
