import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { LuChevronDown, LuChevronRight } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import {
  useThinkingDisplay,
  type ThinkingDisplayMode,
} from '@/lib/show-thinking-content';
import { Markdown } from './Markdown';
import { ToolsFlow } from './ExecutedActionsCard';
import { processStatusLabel, thinkingFoldLabel } from './process-status';
import { splitTurnProcess, type TurnBlock } from './turn-timeline';

/**
 * Codex / DeepSeek-style turn process. Each LLM round keeps three surfaces
 * distinct: 思考 (muted CoT), 思考后的 response (reply bubble), 工具
 * (cards). Trailing text becomes 最后一次结论 only after the stream ends —
 * promoting it earlier would restyle it as thinking when the next round
 * starts. Settings 「显示思考内容」: hidden / 5-line peek / full.
 */

export const THINKING_PEEK_LINES = 5;
/**
 * 5 行 `text-xs` + `leading-relaxed` 的稳定高度。
 * 不用 CSS `lh`：Windows Electron 在中文字体尚未就绪时 `lh` 会算成 0，
 * 思考 peek 整块消失，字体加载后又把主列表高度撑跳。
 */
export const THINKING_PEEK_HEIGHT = `${THINKING_PEEK_LINES * 1.625}em`;

function ReasoningBody({
  content,
  showCursor,
}: {
  content: string;
  showCursor?: boolean;
}) {
  return (
    <>
      <div className="whitespace-pre-wrap break-words">{content}</div>
      {showCursor ? (
        <span className="ml-0.5 inline-block h-3 w-[2px] animate-agent-cursor-blink bg-agent-muted-foreground/60 align-text-bottom" />
      ) : null}
    </>
  );
}

function resolveThinkingDisplay(
  preference: ThinkingDisplayMode,
  thinkingDisplay?: ThinkingDisplayMode,
  showThinkingContent?: boolean,
): ThinkingDisplayMode {
  if (thinkingDisplay) return thinkingDisplay;
  if (showThinkingContent === true) return 'full';
  if (showThinkingContent === false) return 'peek';
  return preference;
}

function useLiveNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [enabled]);
  return now;
}

function ThinkingFold({
  content,
  label,
  showCursor,
  collapsible,
  defaultOpen,
}: {
  content: string;
  label: string;
  showCursor?: boolean;
  collapsible: boolean;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => {
    setOpen(defaultOpen);
  }, [defaultOpen]);

  const body = (
    <div className="border-l border-agent-border/70 pl-3 text-xs leading-relaxed text-agent-muted-foreground">
      <ReasoningBody content={content} showCursor={showCursor} />
    </div>
  );

  if (!collapsible) {
    return (
      <div className="space-y-1" data-testid="turn-thinking">
        <div className="text-[11px] text-agent-muted-foreground/80">{label}</div>
        {body}
      </div>
    );
  }

  return (
    <div className="space-y-1" data-testid="turn-thinking">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex min-w-0 items-center gap-1 py-0.5 text-left text-[11px] text-agent-muted-foreground/80 transition-colors hover:text-agent-foreground"
        aria-expanded={open}
        data-thinking-fold=""
      >
        {open ? (
          <LuChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <LuChevronRight className="h-3 w-3 shrink-0" />
        )}
        <span className="truncate">{label}</span>
      </button>
      {open ? body : null}
    </div>
  );
}

function ProcessBlockItems({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
  foldThinking = false,
  thinkingDefaultOpen = false,
  thinkingElapsedByIndex,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  foldThinking?: boolean;
  thinkingDefaultOpen?: boolean;
  thinkingElapsedByIndex?: Array<number | undefined>;
}) {
  const lastIndex = blocks.length - 1;
  return (
    <>
      {blocks.map((block, index) => {
        const isLast = index === lastIndex;
        if (block.type === 'reasoning') {
          const isLive = isStreaming && isLast;
          return (
            <ThinkingFold
              key={`reasoning-${index}`}
              content={block.content}
              label={thinkingFoldLabel({
                content: block.content,
                isLive,
                elapsedMs: thinkingElapsedByIndex?.[index],
              })}
              showCursor={isLive}
              collapsible={foldThinking}
              defaultOpen={thinkingDefaultOpen || isLive}
            />
          );
        }
        if (block.type === 'tools') {
          return (
            <div key={`tools-${index}`} data-testid="turn-tools">
              <ToolsFlow actions={block.actions} compact />
            </div>
          );
        }
        return (
          <div key={`text-${index}`} data-testid="turn-response">
            <div className="rounded-agent-lg border border-agent-border bg-agent-canvas p-2.5 shadow-sm">
              <div className="markdown-content text-sm leading-relaxed text-agent-foreground">
                <Markdown agents={agents} chats={chats} chatId={chatId}>{block.content}</Markdown>
              </div>
              {isStreaming && isLast ? (
                <span className="ml-0.5 inline-block h-3.5 w-[3px] animate-agent-cursor-blink bg-agent-foreground/60 align-text-bottom" />
              ) : null}
            </div>
          </div>
        );
      })}
    </>
  );
}

function ProcessBlocks({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
  thinkingDefaultOpen,
  thinkingElapsedByIndex,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  thinkingDefaultOpen: boolean;
  thinkingElapsedByIndex?: Array<number | undefined>;
}) {
  return (
    <div className="space-y-2">
      <ProcessBlockItems
        blocks={blocks}
        isStreaming={isStreaming}
        agents={agents}
        chats={chats}
        chatId={chatId}
        foldThinking
        thinkingDefaultOpen={thinkingDefaultOpen}
        thinkingElapsedByIndex={thinkingElapsedByIndex}
      />
    </div>
  );
}

function processPeekSignature(blocks: TurnBlock[]): string {
  return blocks
    .map((block) => {
      if (block.type === 'tools') return `t${block.actions.length}`;
      return `${block.type[0]}${block.content.length}`;
    })
    .join(',');
}

function ThinkingPeek({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
  thinkingElapsedByIndex,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  thinkingElapsedByIndex?: Array<number | undefined>;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const sig = processPeekSignature(blocks);
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [sig]);
  return (
    <div
      ref={scrollerRef}
      className="space-y-2 overflow-y-auto overflow-anchor-none [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
      style={{ height: THINKING_PEEK_HEIGHT }}
      data-thinking-peek=""
      data-testid="thinking-peek"
      data-peek-lines={THINKING_PEEK_LINES}
    >
      <ProcessBlockItems
        blocks={blocks}
        isStreaming={isStreaming}
        agents={agents}
        chats={chats}
        chatId={chatId}
        thinkingElapsedByIndex={thinkingElapsedByIndex}
      />
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
  thinkingDisplay: thinkingDisplayProp,
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
  /** Override the settings preference (hidden / peek / full). */
  thinkingDisplay?: ThinkingDisplayMode;
  /**
   * Legacy override. `true` = full, `false` = peek.
   * Task process panel passes true so the inspector stays expanded.
   */
  showThinkingContent?: boolean;
  renderAnswer: (block: Extract<TurnBlock, { type: 'text' }>, isLast: boolean) => ReactNode;
}) {
  const preference = useThinkingDisplay();
  const thinkingDisplay = resolveThinkingDisplay(
    preference,
    thinkingDisplayProp,
    showThinkingContentProp,
  );
  const showFullThinking = thinkingDisplay === 'full';
  const showThinkingPeekAllowed = thinkingDisplay === 'peek';
  const { process, answer } = splitTurnProcess(blocks, { finalize: !isStreaming });
  const [open, setOpen] = useState(() => showFullThinking);
  const wasStreamingRef = useRef(isStreaming);
  const isStreamingRef = useRef(isStreaming);
  const answerLenRef = useRef(answer.length);
  isStreamingRef.current = isStreaming;
  answerLenRef.current = answer.length;
  const now = useLiveNow(isStreaming);
  const streamClockRef = useRef<number | undefined>(undefined);
  if (isStreaming) {
    streamClockRef.current ??= startedAtMs ?? now;
  } else {
    streamClockRef.current = undefined;
  }
  const liveElapsedMs = isStreaming
    ? Math.max(0, now - (startedAtMs ?? streamClockRef.current ?? now))
    : undefined;
  const lastLiveElapsedRef = useRef<number | undefined>(undefined);
  if (isStreaming && liveElapsedMs != null) {
    lastLiveElapsedRef.current = liveElapsedMs;
  }
  const elapsedMs = isStreaming
    ? liveElapsedMs
    : durationMs ?? lastLiveElapsedRef.current;

  const lastProcess = process[process.length - 1];
  const lastIsReasoning = lastProcess?.type === 'reasoning';
  const reasoningDurationsRef = useRef<number[]>([]);
  const reasoningClockRef = useRef<{ index: number; startedAt: number } | null>(null);
  if (isStreaming && lastIsReasoning) {
    const index = process.length - 1;
    if (reasoningClockRef.current?.index !== index) {
      const prev = reasoningClockRef.current;
      if (prev) {
        reasoningDurationsRef.current[prev.index] = Math.max(0, now - prev.startedAt);
      }
      reasoningClockRef.current = { index, startedAt: now };
    }
  } else if (reasoningClockRef.current) {
    const prev = reasoningClockRef.current;
    reasoningDurationsRef.current[prev.index] = Math.max(0, now - prev.startedAt);
    reasoningClockRef.current = null;
  }
  const thinkingElapsedByIndex = process.map((block, index) => {
    if (block.type !== 'reasoning') return undefined;
    if (
      isStreaming &&
      lastIsReasoning &&
      index === process.length - 1 &&
      reasoningClockRef.current
    ) {
      return Math.max(0, now - reasoningClockRef.current.startedAt);
    }
    return reasoningDurationsRef.current[index];
  });

  useEffect(() => {
    setOpen(showFullThinking);
  }, [showFullThinking]);

  useEffect(() => {
    const wasStreaming = wasStreamingRef.current;
    // peek / hidden fold after the summary lands. full stays open so every
    // round's thinking and tool result remains on screen (hint: 结束后可手动折叠).
    if (wasStreaming && !isStreaming && answer.length > 0 && !showFullThinking) {
      setOpen(false);
    }
    if (!wasStreaming && isStreaming) {
      setOpen(showFullThinking);
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming, answer.length, showFullThinking]);

  if (blocks.length === 0) return <>{emptyFallback}</>;

  const showToggle = process.length > 0;
  const showProcess = !showToggle || open;
  const showThinkingPeek =
    showThinkingPeekAllowed &&
    isStreaming &&
    !open &&
    showToggle;
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
          data-testid="turn-process-toggle"
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
          thinkingDefaultOpen={showFullThinking}
          thinkingElapsedByIndex={thinkingElapsedByIndex}
        />
      )}
      {showThinkingPeek && (
        <ThinkingPeek
          blocks={process}
          isStreaming={answer.length === 0}
          agents={agents}
          chats={chats}
          chatId={chatId}
          thinkingElapsedByIndex={thinkingElapsedByIndex}
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
