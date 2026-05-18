/**
 * `<ThinkingProcessCard />`
 *
 * Collapsible chain-of-thought panel.
 *
 * Behaviour switches:
 *  - `isStreaming` paints the card with a soft pulsing border to convey
 *    "thoughts still arriving".
 *  - `autoExpandWhileStreaming` opens the panel automatically when streaming
 *    starts; `autoCollapseOnComplete` closes it once streaming finishes.
 *  - Controlled mode: pass `expanded` + `onExpandedChange` to delegate the
 *    open/close state (used by host apps that persist expansion in a
 *    context like deeppath's `MessageListContext`).
 *
 * Slots:
 *  - `renderMarkdown(body)`: replace the default `<pre>` body renderer.
 *  - `renderLabel({ isStreaming, isExpanded })`: replace the trigger label
 *    (deeppath uses "正在思考：...", "思考过程", "下一步推理"... variants).
 *  - `renderStreamingFooter`: extra content rendered below the body while
 *    streaming (deeppath shows a pulsing "思考中..." line).
 *  - `bodyClassName` overrides default compact body typography.
 */
import * as React from 'react';
import type { ThinkingProcessPayload } from '@steerable/agent-protocol';
import { ChevronDownIcon, ChevronRightIcon } from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

export interface ThinkingProcessLabelArgs {
  isStreaming: boolean;
  isExpanded: boolean;
}

export interface ThinkingProcessCardProps {
  payload: ThinkingProcessPayload;
  renderMarkdown?: (body: string) => RenderSlotResult;
  renderLabel?: (args: ThinkingProcessLabelArgs) => RenderSlotResult;
  renderStreamingFooter?: () => RenderSlotResult;
  className?: string;
  bodyClassName?: string;
  isStreaming?: boolean;
  expanded?: boolean;
  onExpandedChange?: (next: boolean) => void;
  autoExpandWhileStreaming?: boolean;
  autoCollapseOnComplete?: boolean;
  durationMs?: number | null;
}

function formatDuration(ms?: number | null): string | null {
  if (!ms || ms <= 0) return null;
  return `${(ms / 1000).toFixed(1)}秒`;
}

export const ThinkingProcessCard: React.FC<ThinkingProcessCardProps> = ({
  payload,
  renderMarkdown,
  renderLabel,
  renderStreamingFooter,
  className,
  bodyClassName,
  isStreaming = false,
  expanded: expandedProp,
  onExpandedChange,
  autoExpandWhileStreaming = false,
  autoCollapseOnComplete = false,
  durationMs = null,
}) => {
  const isControlled = expandedProp !== undefined;
  const [internalExpanded, setInternalExpanded] = React.useState(
    Boolean(payload.defaultExpanded),
  );
  const expanded = isControlled ? Boolean(expandedProp) : internalExpanded;
  const setExpanded = React.useCallback(
    (next: boolean) => {
      if (!isControlled) setInternalExpanded(next);
      onExpandedChange?.(next);
    },
    [isControlled, onExpandedChange],
  );

  const prevStreamingRef = React.useRef(isStreaming);
  React.useEffect(() => {
    const wasStreaming = prevStreamingRef.current;
    if (autoExpandWhileStreaming && isStreaming && !expanded && payload.body?.trim()) {
      setExpanded(true);
    }
    if (autoCollapseOnComplete && wasStreaming && !isStreaming && expanded) {
      setExpanded(false);
    }
    prevStreamingRef.current = isStreaming;
  }, [
    isStreaming,
    expanded,
    autoExpandWhileStreaming,
    autoCollapseOnComplete,
    payload.body,
    setExpanded,
  ]);

  const showBody = expanded || (isStreaming && Boolean(payload.body));
  const duration = formatDuration(durationMs);

  const defaultLabel = isStreaming ? '正在思考...' : '思考过程';
  const labelNode = renderLabel
    ? asReactNode(renderLabel({ isStreaming, isExpanded: expanded }))
    : defaultLabel;

  const defaultBodyClasses =
    'whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]';

  return (
    <div className={['steerable-thinking-process', className].filter(Boolean).join(' ')}>
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={[
          'flex items-center gap-1 text-xs transition-colors duration-200',
          isStreaming
            ? 'font-medium text-[var(--agent-foreground,#111827)]'
            : 'text-[var(--agent-muted-foreground,#6b7280)] hover:text-[var(--agent-foreground,#111827)]',
        ].join(' ')}
      >
        {expanded ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
        <span>{labelNode}</span>
        {duration && !isStreaming && (
          <span className="ml-1 text-[var(--agent-muted-foreground,#6b7280)]">({duration})</span>
        )}
        {isStreaming && !expanded && (
          <span
            aria-hidden
            className="ml-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--agent-muted-foreground,#6b7280)]"
          />
        )}
      </button>
      {showBody && (
        <div
          className={[
            'mt-1 overflow-hidden rounded-lg border p-3',
            isStreaming
              ? 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/30 shadow-sm animate-pulse'
              : 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)]/30',
          ].join(' ')}
        >
          <div className={bodyClassName ?? defaultBodyClasses}>
            {renderMarkdown ? asReactNode(renderMarkdown(payload.body)) : payload.body}
          </div>
          {isStreaming && renderStreamingFooter && (
            <div className="mt-2">{asReactNode(renderStreamingFooter())}</div>
          )}
        </div>
      )}
    </div>
  );
};

export default ThinkingProcessCard;
