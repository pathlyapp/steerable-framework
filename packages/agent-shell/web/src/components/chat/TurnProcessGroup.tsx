import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { LuChevronDown, LuChevronRight } from 'react-icons/lu';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import {
  useThinkingDisplay,
  type ThinkingDisplayMode,
} from '@/lib/show-thinking-content';
import { Markdown } from './Markdown';
import { ToolsFlow } from './ExecutedActionsCard';
import {
  estimateReasoningDurationMs,
  processStatusLabel,
  thinkingFoldLabel,
} from './process-status';
import { splitTurnProcess, type TurnBlock } from './turn-timeline';

/**
 * Codex / DeepSeek-style turn process. Each LLM round keeps three surfaces
 * distinct: 思考 (muted CoT), 思考后的 response (reply bubble), 工具
 * (cards). Trailing text becomes 最后一次结论 only after the stream ends —
 * promoting it earlier would restyle it as thinking when the next round
 * starts. Settings 「显示思考内容」 only clip the 思考正文: hidden /
 * 5-line peek / full. A finished 思考 fold auto-collapses in chat;
 * only the live round stays open. Tools and the work row stay independent.
 */

export const THINKING_PEEK_LINES = 5;
/** 思考正文行高（`leading-snug`）。 */
const THINKING_LINE_HEIGHT = 1.375;
/**
 * 最多 5 行：用 max-height，短思考按内容收，不撑空。
 * 不用 CSS `lh`：Windows Electron 在中文字体尚未就绪时 `lh` 会算成 0，
 * 思考 peek 整块消失，字体加载后又把主列表高度撑跳。
 */
export const THINKING_PEEK_HEIGHT = `${THINKING_PEEK_LINES * THINKING_LINE_HEIGHT}em`;

const THINKING_TEXT =
  'border-l border-agent-border/70 pl-2 text-xs leading-snug text-agent-muted-foreground';

function splitThinkingParagraphs(content: string): string[] {
  return content.split(/\n{2,}/).filter((part) => part.length > 0);
}

function ReasoningBody({
  content,
  showCursor,
}: {
  content: string;
  showCursor?: boolean;
}) {
  const parts = splitThinkingParagraphs(content);
  return (
    <>
      <div className="space-y-1">
        {parts.map((part, index) => (
          <div key={index} className="whitespace-pre-wrap break-words">
            {part}
          </div>
        ))}
      </div>
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

function ReasoningPane({
  content,
  showCursor,
  clipped,
}: {
  content: string;
  showCursor?: boolean;
  clipped?: boolean;
}) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!clipped) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [clipped, content]);
  const body = (
    <div className={THINKING_TEXT}>
      <ReasoningBody content={content} showCursor={showCursor} />
    </div>
  );
  if (!clipped) return body;
  return (
    <div
      ref={scrollerRef}
      className="overflow-y-auto overflow-anchor-none [scrollbar-width:none] [-ms-overflow-style:none] [&::-webkit-scrollbar]:hidden"
      style={{ maxHeight: THINKING_PEEK_HEIGHT }}
      data-thinking-peek=""
      data-testid="thinking-peek"
      data-peek-lines={THINKING_PEEK_LINES}
    >
      {body}
    </div>
  );
}

function ThinkingFold({
  content,
  label,
  showCursor,
  mode,
  defaultOpen,
}: {
  content: string;
  label: string;
  showCursor?: boolean;
  mode: ThinkingDisplayMode;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => {
    setOpen(defaultOpen);
  }, [defaultOpen]);

  if (mode === 'hidden') return null;

  return (
    <div className="space-y-1" data-testid="turn-thinking">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex min-w-0 items-center gap-1 py-0.5 text-left text-[11px] text-agent-muted-foreground/80 transition-colors hover:text-agent-foreground"
        aria-expanded={open}
        data-thinking-fold=""
      >
        <span className="truncate">{label}</span>
        {open ? (
          <LuChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <LuChevronRight className="h-3 w-3 shrink-0" />
        )}
      </button>
      {open ? (
        <ReasoningPane
          content={content}
          showCursor={showCursor}
          clipped={mode === 'peek'}
        />
      ) : null}
    </div>
  );
}

function ProcessBlockItems({
  blocks,
  isStreaming,
  agents,
  chats,
  chatId,
  thinkingDisplay,
  keepFinishedThinkingOpen = false,
  thinkingElapsedByIndex,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  thinkingDisplay: ThinkingDisplayMode;
  keepFinishedThinkingOpen?: boolean;
  thinkingElapsedByIndex?: Array<number | undefined>;
}) {
  const lastIndex = blocks.length - 1;
  return (
    <>
      {blocks.map((block, index) => {
        const isLast = index === lastIndex;
        if (block.type === 'reasoning') {
          const isLive = isStreaming && isLast;
          const elapsedMs =
            thinkingElapsedByIndex?.[index]
            ?? block.durationMs
            ?? (isLive ? undefined : estimateReasoningDurationMs(block.content));
          return (
            <ThinkingFold
              key={`reasoning-${index}`}
              content={block.content}
              label={thinkingFoldLabel({
                content: block.content,
                isLive,
                elapsedMs,
              })}
              showCursor={isLive}
              mode={thinkingDisplay}
              defaultOpen={isLive || keepFinishedThinkingOpen}
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
            <div>
              <div className="markdown-content text-xs leading-relaxed text-agent-foreground">
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
  thinkingDisplay,
  keepFinishedThinkingOpen,
  thinkingElapsedByIndex,
}: {
  blocks: TurnBlock[];
  isStreaming: boolean;
  agents: LocalChatAgent[];
  chats: LocalChat[];
  chatId?: string | null;
  thinkingDisplay: ThinkingDisplayMode;
  keepFinishedThinkingOpen: boolean;
  thinkingElapsedByIndex?: Array<number | undefined>;
}) {
  return (
    <div className="space-y-1.5">
      <ProcessBlockItems
        blocks={blocks}
        isStreaming={isStreaming}
        agents={agents}
        chats={chats}
        chatId={chatId}
        thinkingDisplay={thinkingDisplay}
        keepFinishedThinkingOpen={keepFinishedThinkingOpen}
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
  collapseWhenFinished = true,
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
  /**
   * After the stream ends, fold the work row to the summary line.
   * Task inspector sets false so the replay stays open.
   */
  collapseWhenFinished?: boolean;
  renderAnswer: (block: Extract<TurnBlock, { type: 'text' }>, isLast: boolean) => ReactNode;
}) {
  const preference = useThinkingDisplay();
  const thinkingDisplay = resolveThinkingDisplay(
    preference,
    thinkingDisplayProp,
    showThinkingContentProp,
  );
  const showFullThinking = thinkingDisplay === 'full';
  const { process, answer } = splitTurnProcess(blocks, { finalize: !isStreaming });
  const defaultOpen = isStreaming
    ? thinkingDisplay !== 'hidden'
    : showFullThinking && !collapseWhenFinished;
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? defaultOpen;
  useEffect(() => {
    setUserOpen(null);
  }, [isStreaming]);
  const answerLenRef = useRef(answer.length);
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

  if (blocks.length === 0) return <>{emptyFallback}</>;

  const showToggle = process.length > 0;
  const showProcess = !showToggle || open;
  const showStreamingHint =
    Boolean(streamingHint) && isStreaming && answer.length === 0 && !showToggle;

  return (
    <div className="space-y-1" data-turn-timeline>
      {showToggle && (
        <button
          type="button"
          onClick={() => setUserOpen(!open)}
          className="flex w-full items-center gap-1 py-0.5 text-left text-xs text-agent-muted-foreground transition-colors hover:text-agent-foreground"
          aria-expanded={open}
          data-turn-process=""
          data-testid="turn-process-toggle"
          data-open={open || undefined}
        >
          <span className="min-w-0 truncate">
            {processStatusLabel({
              process,
              isStreaming,
              elapsedMs,
            })}
          </span>
          {open ? (
            <LuChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <LuChevronRight className="h-3.5 w-3.5 shrink-0" />
          )}
        </button>
      )}
      {showProcess && (
        <ProcessBlocks
          blocks={process}
          isStreaming={isStreaming && answer.length === 0}
          agents={agents}
          chats={chats}
          chatId={chatId}
          thinkingDisplay={thinkingDisplay}
          keepFinishedThinkingOpen={!collapseWhenFinished}
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
