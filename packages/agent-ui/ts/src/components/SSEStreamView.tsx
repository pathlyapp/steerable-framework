/**
 * `SSEStreamView` — debug / power-user view that prints the raw SSE event log.
 *
 * Used in two production contexts today:
 *   1. The "Trace" tab in `deeppath/apps/web` admin tooling.
 *   2. The mockCflog log window in `deeppath-agent`, where it doubles as a
 *      dev console for the Electron app.
 *
 * The component is intentionally append-only; consumers feed it events via the
 * `events` prop and it auto-scrolls to keep the latest in view.
 */

import { useEffect, useMemo, useRef } from 'react';
import type { SSEEvent } from '@steerable/agent-protocol';
import { cn } from './cn.js';

export interface SSEStreamViewProps {
  events: SSEEvent[];
  className?: string;
  /** Show full JSON payloads instead of a one-line summary. */
  verbose?: boolean;
  autoScroll?: boolean;
  /**
   * Optional filter; only events whose `type` is in the set are rendered.
   * Useful for production debugging where you only care about, e.g.,
   * `tool_call` + `tool_result` chains.
   */
  filterTypes?: SSEEvent['type'][];
  emptyState?: React.ReactNode;
  maxRows?: number;
}

const typeColor: Record<SSEEvent['type'], string> = {
  content: 'text-agent-foreground',
  error: 'text-agent-destructive',
  agent: 'text-agent-accent',
  orchestration: 'text-agent-tool-write',
  'loader-hint': 'text-agent-muted-foreground',
  keepalive: 'text-agent-muted-foreground/60',
  done: 'text-agent-tool-read',
  budget_exhausted: 'text-agent-destructive',
  tool_call: 'text-agent-tool-write',
  tool_result: 'text-agent-tool-read',
};

function summarize(event: SSEEvent): string {
  if (event.type === 'content') {
    const c = typeof event.content === 'string' ? event.content : '';
    return c.length > 80 ? c.slice(0, 80) + '…' : c;
  }
  if (event.type === 'error' || event.type === 'budget_exhausted') {
    return event.message ?? '';
  }
  if (event.type === 'tool_call' || event.type === 'tool_result') {
    const payload = event.payload as Record<string, unknown> | undefined;
    return payload ? JSON.stringify(payload).slice(0, 120) : '';
  }
  if (event.type === 'loader-hint') {
    return event.hint ?? '';
  }
  if (event.type === 'orchestration') {
    return event.orchestrationGroupId ?? '';
  }
  if (event.type === 'agent') {
    return JSON.stringify(event).slice(0, 120);
  }
  return '';
}

export function SSEStreamView(props: SSEStreamViewProps) {
  const {
    events,
    className,
    verbose = false,
    autoScroll = true,
    filterTypes,
    emptyState,
    maxRows = 1000,
  } = props;
  const containerRef = useRef<HTMLDivElement>(null);

  const visible = useMemo(() => {
    let list = filterTypes ? events.filter((e) => filterTypes.includes(e.type)) : events;
    if (list.length > maxRows) {
      list = list.slice(list.length - maxRows);
    }
    return list;
  }, [events, filterTypes, maxRows]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [autoScroll, visible.length]);

  return (
    <div
      ref={containerRef}
      className={cn(
        'h-full overflow-y-auto rounded-agent-md border border-agent-border bg-agent-canvas font-mono text-[11px] leading-relaxed',
        className,
      )}
      role="log"
      aria-live="polite"
    >
      {visible.length === 0 ? (
        <div className="flex h-full items-center justify-center text-agent-muted-foreground">
          {emptyState ?? 'No events yet.'}
        </div>
      ) : (
        <ul className="divide-y divide-agent-border">
          {visible.map((event, idx) => (
            <li
              key={`${idx}_${event.type}`}
              className="flex flex-col gap-0.5 px-3 py-1.5"
            >
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'shrink-0 font-semibold uppercase tracking-wide',
                    typeColor[event.type],
                  )}
                >
                  {event.type}
                </span>
                {!verbose ? (
                  <span className="truncate text-agent-muted-foreground">
                    {summarize(event)}
                  </span>
                ) : null}
              </div>
              {verbose ? (
                <pre className="overflow-x-auto whitespace-pre-wrap break-words text-agent-muted-foreground">
                  {safeStringify(event)}
                </pre>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
