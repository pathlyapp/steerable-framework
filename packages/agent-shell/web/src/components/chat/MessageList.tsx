import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { LuArrowDown } from 'react-icons/lu';
import type { ChatMessage } from '@steerable/agent-protocol';
import type { LocalChat, LocalChatAgent, LocalTask } from '@/lib/local-api';
import { UserMessage } from './UserMessage';
import { AssistantMessage } from './AssistantMessage';
import { InterruptedTurnCard } from './InterruptedTurnCard';
import { TaskOutcomeCards } from './TaskOutcomeCards';
import { SuggestedReplies } from './SuggestedReplies';
import type { ExecutedAction } from './ExecutedActionsCard';
import type { ChildInfo } from './OrchestrationChildrenCard';
import type { ChatMode } from './ChatInput';
import type { TurnBlock } from './turn-timeline';
import type { TurnFile } from './turn-files';
import { inferDurationMs, readPersistedDurationMs } from './elapsed';

/**
 * MessageList — scrolling viewport that renders user/assistant message
 * bubbles.
 *
 * Tier-1 port of `deeppath`'s MessageList. We kept the visible interaction
 * primitives that matter for parity:
 *   - vertical-scroll container, padding mirrors the cloud product
 *   - sticky-ish "back to bottom" floater that appears when the user scrolls
 *     up during streaming
 *   - empty-state slot
 *   - auto-scroll on new message **only if** the user is already near the
 *     bottom (don't yank focus when they're reading older messages)
 *
 * Intentionally simpler than the original:
 *   - **No per-chat scroll memory across tabs.** Switching chats remounts
 *     this list (parent passes a new `key`), so we don't need the
 *     `chatScrollMemoryRef` machinery.
 *   - **No sticky user-message group / `--user-msg-h` CSS var.** The cloud
 *     product uses these to keep the user prompt pinned at the top of the
 *     viewport while reading long assistant replies; agent UX defers that
 *     to a later phase.
 *   - **No load-more on scroll-up.** Local-backend currently hydrates the
 *     last 200 messages on mount (see `AgentPage.tsx`). Pagination is a
 *     phase-2c follow-up.
 *   - **No turn-process toggle memory.** Expand/collapse of the think+tool
 *     group is per-mount; it resets when the list remounts.
 */

const NEAR_BOTTOM_THRESHOLD_PX = 100;

interface MessageListProps {
  messages: ChatMessage[];
  isStreaming: boolean;
  emptyState?: ReactNode;
  agents: LocalChatAgent[];
  chats?: LocalChat[];
  /**
   * 当前对话。正文行内代码里的路径按其绑定项目根解析后，确认存在的变成
   * 可点击（见 FilePathCode）。
   */
  chatId?: string | null;
  currentAgent: LocalChatAgent | null;
  /**
   * Tool calls keyed by the persisted message id (post-stream). Used for
   * historical assistant turns where the `executed_actions` event already
   * fired and was reconciled with the DB-assigned message id.
   */
  executedActionsByMessageId?: Record<string, ExecutedAction[]>;
  /**
   * Call-order blocks keyed by persisted message id. Preferred over stacking
   * all tools above the reply when present.
   */
  timelineByMessageId?: Record<string, TurnBlock[]>;
  /** In-flight timeline for the latest assistant turn. */
  currentTurnTimeline?: TurnBlock[];
  /** Epoch ms when the in-flight turn started (Codex elapsed ticker). */
  currentTurnStartedAtMs?: number;
  /** Frozen duration keyed by assistant message id (live freeze + history). */
  durationByMessageId?: Record<string, number>;
  /**
   * 回合产物文件列表，按落库消息 id 键控（live 回合在 message_id 事件时
   * 归档；历史回合从 messageMetadata.turnFiles 水合）。
   */
  turnFilesByMessageId?: Record<string, TurnFile[]>;
  /**
   * 当轮产物文件：turn_files 事件在流尾声到达，此时尾部助手消息仍挂着
   * 占位 id（框架不会在 message_id 后改写它），归档 map 按键查不到——
   * 与 currentTurnActions 同款尾部回退。
   */
  currentTurnFiles?: TurnFile[];
  /**
   * Tool calls accumulated for the in-flight assistant message that the
   * backend hasn't assigned a DB id to yet. Rendered under the latest
   * assistant message while `isStreaming === true`.
   */
  currentTurnActions?: ExecutedAction[];
  /**
   * P3.1 orchestration: live child agents of the in-flight turn (rendered
   * as an OrchestrationPlanCard under the latest assistant message), and
   * the per-message reconciliation map mirroring executedActionsByMessageId.
   */
  currentTurnChildren?: ChildInfo[];
  orchestrationChildrenByMessageId?: Record<string, ChildInfo[]>;
  /**
   * Round counter for the in-flight turn. Forwarded to the tail
   * AssistantMessage so its `StreamingStatus` can show "Round 2 · 继续推理...".
   */
  currentRound?: number;
  /** Current chat mode (Agent / Plan). */
  mode?: ChatMode;
  /** W1.2.1: regenerate an assistant turn (fork-preserving). */
  onRegenerate?: (messageId: string) => Promise<void>;
  /**
   * W7-1: the chat's last turn was interrupted (crash/kill — no completion
   * record). Rendered as a tail card offering to continue the turn; hidden
   * while streaming (a live stream is never interrupted).
   */
  interrupted?: boolean;
  /** W7-1: continue the interrupted turn via the backend resume channel. */
  onContinueInterrupted?: () => void;
  /** W7-1: hide the card for this mount (not persisted). */
  onDismissInterrupted?: () => void;
  /**
   * 本次挂载期间跑完的后台任务（新→旧）——尾部终态通知卡的数据源。
   * 后台任务跑在主对话之外，终态不落消息行，卡片是它回到对话里的唯一位置。
   */
  finishedTasks?: LocalTask[];
  /** 点「查看过程」：在右侧栏打开该任务的推理过程。 */
  onInspectTask?: (task: LocalTask) => void;
  /** 点「忽略」：隐藏这条终态通知（本次挂载内，不落库）。 */
  onDismissFinishedTask?: (taskId: string) => void;
  /** 分享当前对话（截图）。只画在最近一条助手消息的时间戳行上。 */
  onShare?: () => Promise<boolean>;
  /**
   * 最近一条助手回复下的下一轮输入建议（WorkBuddy 式）。只在非流式时渲染。
   */
  suggestedReplies?: string[];
  onSelectSuggestion?: (text: string) => void;
}

export function MessageList({
  messages,
  isStreaming,
  emptyState,
  agents,
  chats = [],
  chatId = null,
  currentAgent,
  executedActionsByMessageId,
  currentTurnActions,
  timelineByMessageId,
  currentTurnTimeline,
  currentTurnStartedAtMs,
  durationByMessageId,
  turnFilesByMessageId,
  currentTurnFiles,
  currentTurnChildren,
  orchestrationChildrenByMessageId,
  currentRound,
  mode,
  onRegenerate,
  interrupted = false,
  onContinueInterrupted,
  onDismissInterrupted,
  finishedTasks,
  onInspectTask,
  onDismissFinishedTask,
  onShare,
  suggestedReplies,
  onSelectSuggestion,
}: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const isAtBottomRef = useRef(true);

  const visibleMessages = messages.filter(
    (m) => m.role === 'user' || m.role === 'assistant',
  );
  let lastAssistantId: string | undefined;
  for (let i = visibleMessages.length - 1; i >= 0; i -= 1) {
    if (visibleMessages[i].role === 'assistant') {
      lastAssistantId = visibleMessages[i].id;
      break;
    }
  }

  const checkAtBottom = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_THRESHOLD_PX;
    isAtBottomRef.current = atBottom;
    setIsAtBottom(atBottom);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const el = containerRef.current;
    if (!el) return;
    if (behavior === 'smooth') {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.addEventListener('scroll', checkAtBottom, { passive: true });
    return () => {
      el.removeEventListener('scroll', checkAtBottom);
    };
  }, [checkAtBottom]);

  // Snap to bottom on first paint, regardless of whether the user "was" at
  // the bottom — there's no "before" on mount.
  useLayoutEffect(() => {
    isAtBottomRef.current = true;
    scrollToBottom('auto');
    setIsAtBottom(true);
  }, [scrollToBottom]);

  // Stick to the bottom *before paint* while the user is still anchored.
  // Smooth `scrollIntoView` on every reasoning token paints a frame mid-list
  // then animates down — on Windows the classic scrollbar visibly jumps.
  const lastMessageId = visibleMessages[visibleMessages.length - 1]?.id;
  const lastContentLen = visibleMessages[visibleMessages.length - 1]?.content?.length ?? 0;
  const lastTimelineSig = currentTurnTimeline
    ?.map((block) => (block.type === 'tools' ? `t${block.actions.length}` : `c${block.content.length}`))
    .join('|') ?? '';
  const suggestedSig = suggestedReplies?.join('\0') ?? '';
  useLayoutEffect(() => {
    if (!isAtBottomRef.current) return;
    scrollToBottom('auto');
  }, [lastMessageId, lastContentLen, lastTimelineSig, suggestedSig, scrollToBottom]);

  return (
    <div className="relative flex-1 overflow-hidden">
      <div
        ref={containerRef}
        className="h-full overflow-y-auto overflow-anchor-none px-3 py-3"
      >
        {visibleMessages.length === 0 ? (
          emptyState ?? null
        ) : (
          <div className="mx-auto w-full space-y-1.5">
            {visibleMessages.map((message, index) => {
              const isLast = index === visibleMessages.length - 1;
              if (message.role === 'user') {
                return <UserMessage key={message.id} message={message} agents={agents} chats={chats} />;
              }
              // Action attribution: prefer the per-message map (set when the
              // backend's `message_id` event reconciles the stream-time queue
              // with the DB id). The framework keeps the in-flight assistant's
              // placeholder id even after the backend emits `message_id`, so
              // the current page cannot always key by DB id until reload.
              // Keep showing `currentTurnActions` on the latest assistant turn
              // after streaming ends; it is cleared when the next turn starts.
              const persistedActions =
                executedActionsByMessageId?.[message.id];
              const isStreamingTail = isStreaming && isLast;
              const isCurrentTurnTail =
                isLast && currentTurnActions !== undefined && currentTurnActions.length > 0;
              const actions = persistedActions
                ?? (isStreamingTail || isCurrentTurnTail ? currentTurnActions : undefined);
              const persistedChildren = orchestrationChildrenByMessageId?.[message.id];
              const isChildrenTail =
                isLast && currentTurnChildren !== undefined && currentTurnChildren.length > 0;
              const childList = persistedChildren
                ?? (isStreamingTail || isChildrenTail ? currentTurnChildren : undefined);

              let isPlanMode = false;
              if (message.messageMetadata) {
                try {
                  const meta = JSON.parse(message.messageMetadata);
                  if (meta?.mode === 'plan') {
                    isPlanMode = true;
                  }
                } catch {}
              } else if (isStreamingTail && mode === 'plan') {
                isPlanMode = true;
              }

              const persistedTimeline = timelineByMessageId?.[message.id];
              const isCurrentTimelineTail = isLast && currentTurnTimeline !== undefined;
              const turnTimeline = persistedTimeline
                ?? (isStreamingTail || isCurrentTimelineTail ? currentTurnTimeline : undefined);

              const persistedTurnFiles = turnFilesByMessageId?.[message.id];
              const isTurnFilesTail =
                isLast && currentTurnFiles !== undefined && currentTurnFiles.length > 0;
              const turnFiles = persistedTurnFiles
                ?? (isTurnFilesTail ? currentTurnFiles : undefined);

              const metadataJson =
                typeof message.messageMetadata === 'string'
                  ? message.messageMetadata
                  : undefined;
              let previousUserCreatedAt: string | undefined;
              for (let i = index - 1; i >= 0; i -= 1) {
                if (visibleMessages[i].role === 'user') {
                  previousUserCreatedAt = visibleMessages[i].createdAt;
                  break;
                }
              }
              const durationMs = isStreamingTail
                ? undefined
                : durationByMessageId?.[message.id]
                  ?? readPersistedDurationMs(metadataJson)
                  ?? inferDurationMs(previousUserCreatedAt, message.createdAt);

              return (
                <div key={message.id}>
                  <AssistantMessage
                    message={message}
                    isStreaming={isStreamingTail}
                    agents={agents}
                    chats={chats}
                    chatId={chatId}
                    currentAgent={currentAgent}
                    executedActions={actions}
                    timeline={turnTimeline}
                    orchestrationChildren={childList}
                    currentRound={isStreamingTail ? currentRound : undefined}
                    isPlanMode={isPlanMode}
                    onRegenerate={onRegenerate}
                    startedAtMs={isStreamingTail ? currentTurnStartedAtMs : undefined}
                    durationMs={durationMs}
                    turnFiles={turnFiles}
                    onShare={message.id === lastAssistantId ? onShare : undefined}
                  />
                  {!isStreaming &&
                  message.id === lastAssistantId &&
                  suggestedReplies &&
                  suggestedReplies.length > 0 &&
                  onSelectSuggestion ? (
                    <SuggestedReplies
                      suggestions={suggestedReplies}
                      onSelect={onSelectSuggestion}
                    />
                  ) : null}
                </div>
              );
            })}
            {/* W7-1: 中断提示卡在消息列尾部、与最后一条用户消息同列——
                视觉上隶属于被截断的那一轮，而非全局横幅。流式期间不显示
                （进行中的流不是中断）。 */}
            {interrupted && !isStreaming && onContinueInterrupted && onDismissInterrupted ? (
              <InterruptedTurnCard
                onContinue={onContinueInterrupted}
                onDismiss={onDismissInterrupted}
              />
            ) : null}
            {/* 后台任务终态通知：与中断卡同列在消息列尾部。任务跨 turn 跑，
                结束时间与当前这一轮无关，所以钉在末尾而不是挂到某条消息下。
                流式期间照常显示——任务的结束和主对话在不在说话没有关系。 */}
            {finishedTasks && onInspectTask && onDismissFinishedTask ? (
              <TaskOutcomeCards
                tasks={finishedTasks}
                onInspect={onInspectTask}
                onDismiss={onDismissFinishedTask}
              />
            ) : null}
          </div>
        )}
      </div>

      {!isAtBottom && visibleMessages.length > 0 && (
        <button
          type="button"
          onClick={() => {
            isAtBottomRef.current = true;
            setIsAtBottom(true);
            scrollToBottom('smooth');
          }}
          className="absolute bottom-3 right-3 flex h-8 w-8 items-center justify-center rounded-full border border-agent-border bg-agent-canvas text-agent-foreground shadow-md transition-colors hover:bg-agent-foreground/5"
          title="回到底部"
          aria-label="回到底部"
        >
          <LuArrowDown className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

export default MessageList;
