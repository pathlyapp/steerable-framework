import { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { LuCheck, LuCopy, LuListChecks, LuLoaderCircle, LuRefreshCw, LuShare2 } from 'react-icons/lu';
import type { ChatMessage } from '@steerable/agent-protocol';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';
import { Markdown } from './Markdown';
import { ExecutedActionsCard, type ExecutedAction } from './ExecutedActionsCard';
import { OrchestrationChildrenCard, type ChildInfo } from './OrchestrationChildrenCard';
import { StreamingStatus } from './StreamingStatus';
import { getFriendlyDate } from './timestamp';
import { useCopy } from './useCopy';
import type { TurnBlock } from './turn-timeline';
import { TurnProcessGroup } from './TurnProcessGroup';
import { TurnFilesCard } from './TurnFilesCard';
import type { TurnFile } from './turn-files';

/**
 * AssistantMessage — Tier-1 port of `deeppath`'s assistant-bubble.
 *
 * **Covered**:
 *   - Left-aligned bubble flush with user bubbles (no right-align).
 *   - Agent badge at top showing the agent that authored this message
 *     (color dot + name). Historical messages trust `message.agentId`; the
 *     in-flight tail message falls back to `currentAgent` so the badge shows
 *     up the moment streaming starts (before the DB row gets flushed).
 *   - Turn timeline (reasoning / tool cards / reply text in call order)
 *     reconstructed from the SSE stream. After the trailing summary lands,
 *     think+tool steps collapse into one disclosure (Codex / DeepSeek
 *     turn-process). Legacy messages without a timeline still render the
 *     grouped `ExecutedActionsCard` above the body.
 *   - Markdown body (gfm tables, raw html via the shared `Markdown` helper).
 *   - `StreamingStatus` while in-flight + no content yet — escalates the
 *     label based on round / action count so multi-round runs don't feel
 *     stuck.
 *   - Streaming cursor blink at the tail of the rendered content.
 *   - Hover-revealed copy button + friendly timestamp at bottom-left.
 *
 * **Not here yet** (depends on local-backend emitting structured segments):
 *   - `PlanSteps` / `PlanSelector` / `ResearchPlan` /
 *     `AnalysisDocument` / `QuizCard` / `CoverageReport`. These come from
 *     the cloud product's content-segment pipeline.
 *   - `ActionRenderer` (Goal/Task/Event/Resource/Automation update cards).
 *     Skip per the project's "agent-only" charter (no business entities).
 *   - `SearchSources` (web-search source list under the bubble). Local
 *     agent doesn't run web search in the cloud sense.
 */

interface AssistantMessageProps {
  message: ChatMessage;
  /** True iff this is the most recent message AND the stream is still open. */
  isStreaming: boolean;
  /**
   * Lookup table for agent metadata. The bubble shows the color dot + name
   * for the agent that authored this turn. When the message itself doesn't
   * carry `agentId` (historical / builtin / not-yet-flushed during streaming)
   * we fall back to `currentAgent`.
   */
  agents: LocalChatAgent[];
  chats?: LocalChat[];
  /**
   * 当前对话。正文行内代码里的路径按它绑定的项目根解析相对路径后，
   * 确认存在的会变成可点击（见 FilePathCode）。
   */
  chatId?: string | null;
  /** Active chat-level agent — used as a fallback during streaming. */
  currentAgent: LocalChatAgent | null;
  /**
   * Tool calls that ran during this assistant turn (emitted by local-backend
   * via the `executed_actions` SSE event). On the legacy path they render
   * as a grouped card above the body. With a `timeline` they sit inside the
   * foldable think→act process. Empty / undefined => hidden.
   */
  executedActions?: ExecutedAction[];
  /**
   * Call-order blocks for this turn (reasoning → tools → text). When present
   * the bubble renders them interleaved; otherwise `executedActions` +
   * `content` keep the legacy stacked layout.
   */
  timeline?: TurnBlock[];
  /** P3.1: live child-agent list for this turn (orchestration card). */
  orchestrationChildren?: ChildInfo[];
  /**
   * Current round number for the in-flight turn (1-based). Only meaningful
   * when `isStreaming === true` and this is the tail message — used by
   * `StreamingStatus` to render "Round 2 · 继续推理..." between LLM bursts.
   */
  currentRound?: number;
  /** True iff the message was generated in Plan mode. */
  isPlanMode?: boolean;
  /**
   * W1.2.1: regenerate this assistant turn (non-destructive — the old tail
   * is forked into a branch by the backend). When provided, a hover action
   * appears next to copy. The promise resolves when the regenerated turn
   * has fully streamed; the caller then re-hydrates the message list.
   */
  onRegenerate?: (messageId: string) => Promise<void>;
  /** Epoch ms when this turn started — live ticker while streaming. */
  startedAtMs?: number;
  /** Frozen wall-clock of a finished turn. */
  durationMs?: number;
  /**
   * 本回合产生/修改的文件列表（local-backend 回合收尾时收集，经
   * `turn_files` SSE + messageMetadata.turnFiles 持久化）。渲染在回答
   * 气泡之下，点击用系统默认应用打开。空/undefined = 不渲染。
   */
  turnFiles?: TurnFile[];
  /**
   * 分享当前对话（截图复制到剪贴板）。只传给最近一条助手消息，画在时间戳
   * 行上，跟复制 / 重新生成同一排。
   */
  onShare?: () => Promise<boolean>;
}

function agentInitial(agent: LocalChatAgent | null): string {
  if (!agent?.name) return 'A';
  return agent.name.trim()[0]?.toUpperCase() ?? 'A';
}

function AgentBadge({
  agent,
  isStreaming,
}: {
  agent: LocalChatAgent;
  isStreaming: boolean;
}) {
  return (
    <div
      className="inline-flex items-center gap-1.5 text-[11px] leading-none text-agent-muted-foreground"
      title={agent.description || agent.name}
    >
      <span
        className="inline-flex items-center justify-center rounded-full text-[9px] font-semibold text-white"
        style={{
          width: 14,
          height: 14,
          backgroundColor: agent.color || '#7c3aed',
        }}
      >
        {agentInitial(agent)}
      </span>
      <span className="max-w-[160px] truncate rounded bg-agent-canvas/90 px-1">
        {agent.name}
      </span>
      {isStreaming && (
        <span className="ml-1 inline-flex h-1.5 w-1.5 animate-pulse rounded-full bg-agent-foreground/30" />
      )}
    </div>
  );
}

const META_ACTION =
  'inline-flex items-center gap-0.5 rounded transition-all duration-200 hover:text-agent-foreground opacity-0 focus:opacity-100 group-hover/message:opacity-100';

function bubbleClass(isPlanMode: boolean): string {
  return `rounded-agent-lg border p-3 shadow-sm transition-all duration-200 ${
    isPlanMode
      ? 'border-amber-400/50 dark:border-amber-500/30 bg-amber-50/10 dark:bg-amber-950/5 shadow-amber-500/5'
      : 'border-agent-border bg-agent-canvas'
  }`;
}

// 框架 useChatStream 收到 SSE error 事件时把最后一条助手消息的内容打成
// "[stream error] ..."（live 路径）；落库后失败原因在
// messageMetadata.completionReason（刷新/重载路径，可能还留着部分正文）。
// 两处汇合到同一个错误气泡，失败回合不再只显示"（空消息）"。
const STREAM_ERROR_PREFIX = '[stream error] ';

function readTurnFailure(
  message: ChatMessage,
  content: string,
): { reason: string; contentIsError: boolean } | null {
  if (content.startsWith(STREAM_ERROR_PREFIX)) {
    return { reason: content.slice(STREAM_ERROR_PREFIX.length), contentIsError: true };
  }
  const raw = (message as { messageMetadata?: unknown }).messageMetadata;
  if (typeof raw !== 'string' || !raw) return null;
  try {
    const meta = JSON.parse(raw) as {
      completionStatus?: unknown;
      completionReason?: unknown;
    };
    if (
      meta?.completionStatus === 'failed' &&
      typeof meta.completionReason === 'string' &&
      meta.completionReason
    ) {
      return { reason: meta.completionReason, contentIsError: false };
    }
  } catch {
    // 元数据损坏按无失败处理——落回原有的空消息展示。
  }
  return null;
}

function TurnErrorBubble({ reason }: { reason: string }) {
  return (
    <div className="rounded-agent-lg border border-agent-destructive/40 bg-agent-destructive/5 p-3 text-sm leading-relaxed text-agent-destructive shadow-sm">
      请求失败：{reason}
    </div>
  );
}

function StreamingCursor() {
  return (
    <span className="ml-0.5 inline-block h-3.5 w-[3px] animate-agent-cursor-blink bg-agent-foreground/60 align-text-bottom" />
  );
}

export function AssistantMessage({
  message,
  isStreaming,
  agents,
  chats = [],
  chatId = null,
  currentAgent,
  executedActions,
  timeline,
  orchestrationChildren,
  currentRound = 1,
  isPlanMode = false,
  onRegenerate,
  startedAtMs,
  durationMs,
  turnFiles,
  onShare,
}: AssistantMessageProps) {
  const content = message.content || '';
  const failure = readTurnFailure(message, content);
  // live 错误文本（"[stream error] ..."）不当正文渲染；落库的失败回合可能
  // 留有部分正文，此时正文与错误气泡都展示。
  const displayContent = failure?.contentIsError ? '' : content;
  const { copied, copy } = useCopy(content);
  const [regenerating, setRegenerating] = useState(false);
  const [regenerateError, setRegenerateError] = useState<string | null>(null);
  const persistedAgent = message.agentId
    ? agents.find((a) => a.id === message.agentId) ?? null
    : null;
  // Stream-time fallback: only allow the chat-level agent to "claim" a turn
  // while it's actively streaming and hasn't flushed agentId to the DB yet.
  const displayAgent = persistedAgent ?? (isStreaming ? currentAgent : null);
  const useTimeline = timeline !== undefined;
  const blocks = timeline ?? [];

  return (
    <motion.div
      className="group/message mb-2"
      data-message-role="assistant"
      data-message-id={message.id}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
    >
      <div className="mx-auto w-full max-w-[var(--chat-input-box-width)] px-1">
        {(displayAgent || isPlanMode) && (
          <div className="flex items-center justify-between mb-1.5 min-h-[18px]">
            {displayAgent ? (
              <AgentBadge agent={displayAgent} isStreaming={isStreaming} />
            ) : (
              <div />
            )}
            {isPlanMode && (
              <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 dark:border-amber-800/50 bg-amber-100/70 dark:bg-amber-900/30 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300 select-none">
                <LuListChecks className="h-3.5 w-3.5" />
                <span>计划模式 (Plan)</span>
              </span>
            )}
          </div>
        )}
        {orchestrationChildren && orchestrationChildren.length > 0 && (
          <OrchestrationChildrenCard children={orchestrationChildren} />
        )}
        {useTimeline ? (
          <>
          <TurnProcessGroup
            blocks={blocks}
            isStreaming={isStreaming}
            startedAtMs={startedAtMs}
            durationMs={durationMs}
            agents={agents}
            chats={chats}
            chatId={chatId}
            emptyFallback={
              failure ? (
                <TurnErrorBubble reason={failure.reason} />
              ) : isStreaming ? (
                <div className={bubbleClass(isPlanMode)}>
                  <StreamingStatus
                    round={currentRound}
                    actionCount={executedActions?.length ?? 0}
                    hasContent={false}
                  />
                </div>
              ) : (
                <div className={`${bubbleClass(isPlanMode)} text-sm italic text-agent-muted-foreground`}>
                  (空消息)
                </div>
              )
            }
            streamingHint={
              <StreamingStatus
                round={currentRound}
                actionCount={executedActions?.length ?? 0}
                hasContent={false}
              />
            }
            renderAnswer={(block, isLast) => (
              <div className={bubbleClass(isPlanMode)}>
                <div className="markdown-content text-sm leading-relaxed text-agent-foreground">
                  <Markdown agents={agents} chats={chats} chatId={chatId}>{block.content}</Markdown>
                </div>
                {isStreaming && isLast && <StreamingCursor />}
              </div>
            )}
          />
          {/* 有部分正文/工具块但回合失败：正文照常渲染，错误气泡补在下方
              （无块时走上面的 emptyFallback，不会重复）。 */}
          {failure && blocks.length > 0 ? (
            <div className="mt-1.5">
              <TurnErrorBubble reason={failure.reason} />
            </div>
          ) : null}
          </>
        ) : (
          <>
            {executedActions && executedActions.length > 0 && (
              <ExecutedActionsCard actions={executedActions} />
            )}
            {displayContent ? (
              <div className={bubbleClass(isPlanMode)}>
                <div className="markdown-content text-sm leading-relaxed text-agent-foreground">
                  <Markdown agents={agents} chats={chats} chatId={chatId}>{displayContent}</Markdown>
                </div>
                {isStreaming && <StreamingCursor />}
              </div>
            ) : failure ? (
              <TurnErrorBubble reason={failure.reason} />
            ) : isStreaming ? (
              <div className={bubbleClass(isPlanMode)}>
                <StreamingStatus
                  round={currentRound}
                  actionCount={executedActions?.length ?? 0}
                  hasContent={false}
                />
              </div>
            ) : (
              <div className={`${bubbleClass(isPlanMode)} text-sm italic text-agent-muted-foreground`}>
                (空消息)
              </div>
            )}
            {displayContent && failure ? (
              <div className="mt-1.5">
                <TurnErrorBubble reason={failure.reason} />
              </div>
            ) : null}
          </>
        )}
        {/* 回合产物文件列表：钉在回答之下、时间戳行之上（Codex 式收尾）。
            回合收尾才有数据，流式期间天然为空。 */}
        {!isStreaming && turnFiles && turnFiles.length > 0 && (
          <div className="mt-1.5">
            <TurnFilesCard files={turnFiles} />
          </div>
        )}
        <div className="mt-1 flex items-center gap-2 text-[11px] text-agent-muted-foreground">
          <span>
            {message.createdAt
              ? getFriendlyDate(new Date(message.createdAt))
              : ''}
          </span>
          {content && !isStreaming && (
            <button
              type="button"
              onClick={() => void copy()}
              className={META_ACTION}
              title={copied ? '已复制' : '复制消息'}
              aria-label={copied ? '已复制' : '复制消息'}
            >
              {copied ? (
                <>
                  <LuCheck className="h-3 w-3 text-emerald-600 dark:text-emerald-400" />
                  <span className="text-emerald-600 dark:text-emerald-400">
                    已复制
                  </span>
                </>
              ) : (
                <LuCopy className="h-3 w-3" />
              )}
            </button>
          )}
          {content && !isStreaming && onRegenerate && (
            <button
              type="button"
              disabled={regenerating}
              onClick={() => {
                if (regenerating) return;
                setRegenerating(true);
                setRegenerateError(null);
                onRegenerate(message.id)
                  // The backend refuses when it could not preserve this reply
                  // as a branch. Dropping that leaves the spinner stopping for
                  // no stated reason while the reply sits unchanged.
                  .catch((err: unknown) =>
                    setRegenerateError(
                      err instanceof Error ? err.message : String(err),
                    ),
                  )
                  .finally(() => setRegenerating(false));
              }}
              className={`${META_ACTION} disabled:cursor-not-allowed`}
              title={regenerating ? '正在重新生成…' : '重新生成（旧回复保留为分支）'}
              aria-label={regenerating ? '正在重新生成' : '重新生成'}
              data-action="regenerate"
            >
              <LuRefreshCw
                className={`h-3 w-3 ${regenerating ? 'animate-spin' : ''}`}
              />
            </button>
          )}
          {onShare && !isStreaming && <ShareChatButton onShare={onShare} />}
        </div>
        {regenerateError && (
          <div
            className="mt-1 text-[11px] text-amber-700 dark:text-amber-400"
            role="status"
            data-regenerate-error
          >
            {regenerateError}
          </div>
        )}
      </div>
    </motion.div>
  );
}

export default AssistantMessage;

function ShareChatButton({ onShare }: { onShare: () => Promise<boolean> }) {
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const resetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (resetTimerRef.current) clearTimeout(resetTimerRef.current);
    },
    [],
  );

  return (
    <button
      type="button"
      disabled={state === 'busy'}
      onClick={() => {
        if (state === 'busy') return;
        setState('busy');
        void (async () => {
          let ok = false;
          try {
            ok = await onShare();
          } catch {
            ok = false;
          }
          setState(ok ? 'done' : 'error');
          if (resetTimerRef.current) clearTimeout(resetTimerRef.current);
          resetTimerRef.current = setTimeout(() => setState('idle'), 2000);
        })();
      }}
      className={`${META_ACTION} disabled:cursor-not-allowed ${
        state === 'done'
          ? 'text-emerald-600 dark:text-emerald-400'
          : state === 'error'
            ? 'text-agent-destructive'
            : ''
      }`}
      title={
        state === 'done'
          ? '截图已复制到剪贴板'
          : state === 'error'
            ? '截图失败，请重试'
            : '分享对话（截图复制到剪贴板）'
      }
      aria-label="分享对话截图"
    >
      {state === 'busy' ? (
        <LuLoaderCircle className="h-3 w-3 animate-spin" />
      ) : state === 'done' ? (
        <LuCheck className="h-3 w-3" />
      ) : (
        <LuShare2 className="h-3 w-3" />
      )}
    </button>
  );
}
