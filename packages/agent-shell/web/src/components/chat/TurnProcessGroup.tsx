import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { LuChevronDown, LuChevronRight } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import { Markdown } from './Markdown';
import { ToolsFlow } from './ExecutedActionsCard';
import { formatElapsedCompact } from './elapsed';
import {
  countProcessTools,
  processHasReasoning,
  splitTurnProcess,
  type TurnBlock,
} from './turn-timeline';

/**
 * Codex / DeepSeek-style turn process: think + tool rows stay in one
 * disclosure. Expanded while the turn streams; collapsed once the trailing
 * summary is on screen. The summary itself stays outside the fold.
 * Elapsed time ticks while streaming (Codex status line) and freezes as
 * 「工作了 …」 after the turn ends.
 */

function useLiveElapsedMs(
  startedAtMs: number | undefined,
  enabled: boolean,
): number | undefined {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled || startedAtMs == null) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [enabled, startedAtMs]);
  if (startedAtMs == null) return undefined;
  return Math.max(0, now - startedAtMs);
}

function processLabel(
  process: TurnBlock[],
  isStreaming: boolean,
  elapsedMs: number | undefined,
): string {
  const elapsed =
    elapsedMs != null && (isStreaming || elapsedMs >= 1000)
      ? formatElapsedCompact(elapsedMs)
      : null;
  if (isStreaming) {
    return elapsed ? `正在执行… ${elapsed}` : '正在执行…';
  }
  const parts: string[] = [];
  const tools = countProcessTools(process);
  if (tools > 0) parts.push(`${tools} 次工具调用`);
  if (processHasReasoning(process)) parts.push('已思考');
  if (elapsed) parts.push(`工作了 ${elapsed}`);
  if (parts.length === 0) return '执行过程';
  return parts.join(' · ');
}

function ProcessBlocks({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
}) {
  const lastIndex = blocks.length - 1;
  return (
    <div className="space-y-1.5 border-l border-agent-border/70 pl-3">
      {blocks.map((block, index) => {
        const isLast = index === lastIndex;
        if (block.type === 'reasoning') {
          return (
            <div
              key={`reasoning-${index}`}
              className="text-xs leading-relaxed text-agent-muted-foreground"
            >
              <Markdown agents={agents} chats={chats} chatId={chatId}>{block.content}</Markdown>
              {isStreaming && isLast && (
                <span className="ml-0.5 inline-block h-3 w-[2px] animate-agent-cursor-blink bg-agent-muted-foreground/60 align-text-bottom" />
              )}
            </div>
          );
        }
        if (block.type === 'tools') {
          return <ToolsFlow key={`tools-${index}`} actions={block.actions} compact />;
        }
        return (
          <div
            key={`text-${index}`}
            className="markdown-content text-xs leading-relaxed text-agent-muted-foreground"
          >
            <Markdown agents={agents} chats={chats} chatId={chatId}>{block.content}</Markdown>
          </div>
        );
      })}
    </div>
  );
}

export function TurnProcessGroup({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
  emptyFallback,
  streamingHint,
  startedAtMs,
  durationMs,
  renderAnswer,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  emptyFallback: ReactNode;
  /** Shown while streaming after a tools row and before the summary lands. */
  streamingHint?: ReactNode;
  /** Epoch ms when this turn started — live ticker while `isStreaming`. */
  startedAtMs?: number;
  /** Frozen wall-clock of a finished turn (metadata or just-ended stream). */
  durationMs?: number;
  renderAnswer: (block: Extract<TurnBlock, { type: 'text' }>, isLast: boolean) => ReactNode;
}) {
  const { process, answer } = splitTurnProcess(blocks);
  const [open, setOpen] = useState(() => isStreaming || answer.length === 0);
  const wasStreamingRef = useRef(isStreaming);
  const liveElapsedMs = useLiveElapsedMs(startedAtMs, isStreaming);
  const lastLiveElapsedRef = useRef<number | undefined>(undefined);
  if (isStreaming && liveElapsedMs != null) {
    lastLiveElapsedRef.current = liveElapsedMs;
  }
  const elapsedMs = isStreaming
    ? liveElapsedMs
    : durationMs ?? lastLiveElapsedRef.current;

  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    if (wasStreaming && !isStreaming && answer.length > 0) {
      setOpen(false);
    }
    if (!wasStreaming && isStreaming) {
      setOpen(true);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming, answer.length]);

  if (blocks.length === 0) return <>{emptyFallback}</>;

  const showToggle = process.length > 0;
  const showProcess = !showToggle || open;
  const lastProcess = process[process.length - 1];
  const showStreamingHint =
    Boolean(streamingHint) && isStreaming && answer.length === 0 && lastProcess?.type === 'tools';

  return (
    <div className="space-y-1.5" data-turn-timeline>
      {showToggle && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="flex w-full items-center gap-1 py-0.5 text-left text-xs text-agent-muted-foreground transition-colors hover:text-agent-foreground"
          aria-expanded={open}
          data-turn-process=""
          data-open={open || undefined}
        >
          {open ? (
            <LuChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <LuChevronRight className="h-3.5 w-3.5 shrink-0" />
          )}
          <span className="truncate">{processLabel(process, isStreaming, elapsedMs)}</span>
        </button>
      )}
      {showProcess && (
        <ProcessBlocks
          blocks={process}
          isStreaming={isStreaming && answer.length === 0}
          agents={agents}
          chats={chats}
          chatId={chatId}
        />
      )}
      {showStreamingHint ? streamingHint : null}
      {answer.map((block, index) => (
        <Fragment key={`answer-${index}`}>
          {renderAnswer(block, index === answer.length - 1)}
        </Fragment>
      ))}
    </div>
  );
}
