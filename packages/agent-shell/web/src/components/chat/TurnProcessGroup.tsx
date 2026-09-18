import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { LuChevronDown, LuChevronRight } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import { useShowThinkingContent } from '@/lib/show-thinking-content';
import { Markdown } from './Markdown';
import { ToolsFlow } from './ExecutedActionsCard';
import { processStatusLabel } from './process-status';
import { processHasReasoning, splitTurnProcess, type TurnBlock } from './turn-timeline';

/**
 * Codex / DeepSeek-style turn process: think + tool rows stay in one
 * disclosure. Default is collapsed (settings 「显示思考内容」 off). While
 * streaming, a fixed 7-line peek shows the latest reasoning; it folds when
 * the turn ends. The status line still shows 思考中 / 工具名 / tok/s /
 * elapsed. The summary itself stays outside the fold.
 */

export const THINKING_PEEK_LINES = 7;

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

function ThinkingPeek({
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
  const scrollerRef = useRef<HTMLDivElement>(null);
  const reasoning = blocks.filter(
    (block) => block.type === 'reasoning' && block.content.trim().length > 0,
  );
  const sig = reasoning.map((block) => block.content.length).join(',');
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [sig]);
  const lastIndex = reasoning.length - 1;
  return (
    <div
      ref={scrollerRef}
      className="h-[7lh] overflow-hidden border-l border-agent-border/70 pl-3 text-xs leading-relaxed text-agent-muted-foreground"
      data-thinking-peek=""
      data-testid="thinking-peek"
      data-peek-lines={THINKING_PEEK_LINES}
    >
      {reasoning.map((block, index) => (
        <div key={`peek-reasoning-${index}`}>
          <Markdown agents={agents} chats={chats} chatId={chatId}>{block.content}</Markdown>
          {isStreaming && index === lastIndex && (
            <span className="ml-0.5 inline-block h-3 w-[2px] animate-agent-cursor-blink bg-agent-muted-foreground/60 align-text-bottom" />
          )}
        </div>
      ))}
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
  showThinkingContent: showThinkingContentProp,
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
  /**
   * Override the settings preference. Omitted = read 「显示思考内容」.
   * Task process panel passes true so the inspector stays expanded.
   */
  showThinkingContent?: boolean;
  renderAnswer: (block: Extract<TurnBlock, { type: 'text' }>, isLast: boolean) => ReactNode;
}) {
  const preference = useShowThinkingContent();
  const showThinkingContent = showThinkingContentProp ?? preference;
  const { process, answer } = splitTurnProcess(blocks);
  const [open, setOpen] = useState(
    () => showThinkingContent && (isStreaming || answer.length === 0),
  );
  const wasStreamingRef = useRef(isStreaming);
  const isStreamingRef = useRef(isStreaming);
  const answerLenRef = useRef(answer.length);
  isStreamingRef.current = isStreaming;
  answerLenRef.current = answer.length;
  const liveElapsedMs = useLiveElapsedMs(startedAtMs, isStreaming);
  const lastLiveElapsedRef = useRef<number | undefined>(undefined);
  if (isStreaming && liveElapsedMs != null) {
    lastLiveElapsedRef.current = liveElapsedMs;
  }
  const elapsedMs = isStreaming
    ? liveElapsedMs
    : durationMs ?? lastLiveElapsedRef.current;

  const lastProcess = process[process.length - 1];
  const reasoningClockRef = useRef<{ index: number; startedAt: number } | null>(null);
  if (isStreaming && lastProcess?.type === 'reasoning') {
    const index = process.length - 1;
    if (reasoningClockRef.current?.index !== index) {
      reasoningClockRef.current = { index, startedAt: Date.now() };
    }
  } else if (!isStreaming) {
    reasoningClockRef.current = null;
  }
  const reasoningElapsedMs =
    isStreaming && lastProcess?.type === 'reasoning' && reasoningClockRef.current
      ? Math.max(0, Date.now() - reasoningClockRef.current.startedAt)
      : undefined;

  useEffect(() => {
    if (!showThinkingContent) {
      setOpen(false);
      return;
    }
    if (isStreamingRef.current || answerLenRef.current === 0) setOpen(true);
  }, [showThinkingContent]);

  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    if (wasStreaming && !isStreaming && answer.length > 0) {
      setOpen(false);
    }
    if (!wasStreaming && isStreaming) {
      setOpen(showThinkingContent);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming, answer.length, showThinkingContent]);

  if (blocks.length === 0) return <>{emptyFallback}</>;

  const showToggle = process.length > 0;
  const showProcess = !showToggle || open;
  const showThinkingPeek =
    isStreaming && !open && showToggle && processHasReasoning(process);
  const showStreamingHint =
    Boolean(streamingHint) && isStreaming && answer.length === 0 && !showToggle;

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
          <span className="truncate">
            {processStatusLabel({
              process,
              isStreaming,
              elapsedMs,
              reasoningElapsedMs,
            })}
          </span>
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
      {showThinkingPeek && (
        <ThinkingPeek
          blocks={process}
          isStreaming={answer.length === 0}
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
