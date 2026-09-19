import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { ChatPanel } from '@steerable/agent-ui';
import type { SteerOutcome } from '@steerable/agent-ui';
import type { LocalChat, LocalChatAgent, LocalTask } from '@/lib/local-api';
import {
  ChatInput,
  type ChatInputHandle,
  type ChatMode,
  type ExecPolicy,
  type MentionReference,
} from './ChatInput';
import { EmptyChat } from './EmptyChat';
import { MessageList } from './MessageList';
import type { ExecutedAction } from './ExecutedActionsCard';
import type { ChildInfo } from './OrchestrationChildrenCard';
import type { TurnBlock } from './turn-timeline';
import type { TurnFile } from './turn-files';
import type { LlmSpeedSnapshot } from './process-status';
import {
  isImageFile,
  saveChatAttachments,
  type AttachmentFile,
} from '@/lib/attachments';

/**
 * LocalChatPanel — the product chat shell.
 *
 * History:
 *   - Pre-0.3 of `@steerable/agent-ui`, the framework's `ChatPanel` was a
 *     barebones monolith with no slots — so this file shipped a complete
 *     parallel implementation.
 *   - 0.3 introduced the compound API (`ChatPanel.Root/.Header/.Messages/.
 *     Input/.Empty`). This file is now a thin facade that plugs in the local
 *     rich renderers (`MessageList` with agent badges + executed-actions
 *     cards, `ChatInput` with the meta-row agent picker + settings gear)
 *     into the framework's structural shell.
 *
 * Props mirror the framework's `ChatPanelProps` so the swap in `AgentPage`
 * remained a single import change. Two extras are accepted:
 *   - `agents` — agent catalog for the assistant bubble badge.
 *   - `currentAgent` — chat-level agent, used as the badge fallback while
 *     the in-flight assistant turn hasn't flushed its `agentId` to the DB.
 *
 * Input value is lifted here (not inside `ChatInput`) so the empty-state
 * `EmptyChat` can call back into us to set the textarea contents — same UX
 * as the cloud product's `setAndFocusInputMessage`.
 */

export interface LocalChatPanelProps {
  messages: ChatMessage[];
  isStreaming?: boolean;
  onSubmit: (input: { content: string; metadata?: Record<string, unknown> }) => void | Promise<void>;
  onCancel?: () => void;
  /** 轮中转向（streaming 期间 Enter）：注入失败由 hook 兜底为排队/直发，见 ChatInput。 */
  onSteer?: (text: string) => Promise<SteerOutcome>;
  /** W6-2 follow-up 排队：streaming 期间 ⌘/Ctrl+Enter 把文本排入待发队列。 */
  onFollowUp?: (text: string) => void;
  /** 排队待发的 follow-up 文本列表。 */
  pendingFollowUps?: string[];
  /** 撤回第 N 条排队中的 follow-up。 */
  onRemoveFollowUp?: (index: number) => void;
  className?: string;
  inputPlaceholder?: string;
  /**
   * 当前会话 id。提供后，提交时上传的附件会被持久化到会话空间
   * （`saveChatAttachments`），消息正文与图像元数据用落盘路径。落地页
   * （尚无 chatId）可省略，由调用方在创建会话后自行持久化。
   */
  chatId?: string | null;
  /** Slot rendered above the message list (header / agent picker / …). */
  header?: ReactNode;
  /**
   * 空会话 hero 布局：提供后，当 messages 为空且未在流式时，整个面板渲染为
   * 居中首屏（品牌标题 + 居中输入框，无 header）——与 /agent 落地页
   * （EmptyChatGate）同款视觉。首条消息落地后自动切回常规布局。
   */
  emptyHero?: { title: string; subtitle?: string };
  /** Override the default `EmptyChat` empty-state. */
  emptyState?: ReactNode;
  /** Disable the input (e.g. during initial chat-history load). */
  disabled?: boolean;

  agents: LocalChatAgent[];
  chats?: LocalChat[];
  currentAgent: LocalChatAgent | null;
  selectedAgentId?: string | null;
  onSelectAgent?: (agentId: string) => void | Promise<void>;

  /**
   * Tool calls keyed by persisted assistant message id. Forwarded straight
   * to `MessageList`. See its prop docs for the in-flight vs historical
   * split.
   */
  executedActionsByMessageId?: Record<string, ExecutedAction[]>;
  /** In-flight tool calls — rendered under the latest assistant bubble. */
  currentTurnActions?: ExecutedAction[];
  timelineByMessageId?: Record<string, TurnBlock[]>;
  currentTurnTimeline?: TurnBlock[];
  currentTurnStartedAtMs?: number;
  durationByMessageId?: Record<string, number>;
  currentLlmSpeed?: LlmSpeedSnapshot;
  llmSpeedByMessageId?: Record<string, LlmSpeedSnapshot>;
  /** 回合产物文件列表（按落库消息 id 键控），转发给 MessageList。 */
  turnFilesByMessageId?: Record<string, TurnFile[]>;
  /** 当轮产物文件（流尾声到达、尚未归档到落库 id 的尾部消息用）。 */
  currentTurnFiles?: TurnFile[];
  /** P3.1: in-flight child agents + per-message reconciliation map. */
  currentTurnChildren?: ChildInfo[];
  orchestrationChildrenByMessageId?: Record<string, ChildInfo[]>;
  /** Current round number for the in-flight turn (1-based). */
  currentRound?: number;
  /**
   * Settings gear callback for the input toolbar. Receiving this enables
   * the gear button on `ChatInput`; AgentPage wires it to opening
   * `LocalLlmSettingsModal`. Optional so the panel still works in places
   * (preview harness, unit tests) without modal infra.
   */
  onOpenSettings?: () => void;
  /** Extra controls on the input's left toolbar (AgentPage mounts the
   * model/effort picker here). Forwarded to ChatInput's `toolbarExtras`. */
  inputToolbarExtras?: ReactNode;
  /** Slot in the composer meta row above the input box, before the agent
   * picker (AgentPage mounts the project badge here). */
  inputLeadingChrome?: ReactNode;

  /** Current chat mode (Agent / Plan). When unset, the toggle is hidden. */
  mode?: ChatMode;
  /** Called when the user switches modes. */
  onModeChange?: (mode: ChatMode) => void;
  /** 命令沙箱档。未传 onExecPolicyChange 时不显示选择器。 */
  execPolicy?: ExecPolicy;
  onExecPolicyChange?: (policy: ExecPolicy) => void;
  /** Slot rendered just above the input box (e.g. "执行此计划" 操作条). */
  inputBanner?: ReactNode;
  /** W1.2.1: regenerate an assistant turn (fork-preserving). */
  onRegenerate?: (messageId: string) => Promise<void>;
  /** W7-1: 上一轮被中断（崩溃/强杀）——消息列表尾部展示「继续上次回复」卡片。 */
  interrupted?: boolean;
  /** W7-1: 点击「继续上次回复」。 */
  onContinueInterrupted?: () => void;
  /** W7-1: 点击「忽略」（本次挂载内隐藏，不落库）。 */
  onDismissInterrupted?: () => void;
  /** 本次挂载期间跑完的后台任务——消息列尾部的终态通知卡。 */
  finishedTasks?: LocalTask[];
  /** 终态卡「查看过程」：在右侧栏打开该任务的推理过程。 */
  onInspectTask?: (task: LocalTask) => void;
  /** 终态卡「忽略」（本次挂载内隐藏，不落库）。 */
  onDismissFinishedTask?: (taskId: string) => void;
  /** 分享当前对话（截图）。落到最近一条助手消息的时间戳行。 */
  onShare?: () => Promise<boolean>;
  /**
   * 最近一条助手回复下的下一轮输入建议。点击即作为新用户消息发出。
   */
  suggestedReplies?: string[];
  onSelectSuggestion?: (text: string) => void;
}

export function LocalChatPanel({
  messages,
  isStreaming = false,
  onSubmit,
  onCancel,
  className,
  inputPlaceholder,
  chatId,
  header,
  emptyHero,
  emptyState,
  disabled = false,
  agents,
  chats = [],
  currentAgent,
  selectedAgentId,
  onSelectAgent,
  executedActionsByMessageId,
  currentTurnActions,
  timelineByMessageId,
  currentTurnTimeline,
  currentTurnStartedAtMs,
  durationByMessageId,
  currentLlmSpeed,
  llmSpeedByMessageId,
  turnFilesByMessageId,
  currentTurnFiles,
  currentTurnChildren,
  orchestrationChildrenByMessageId,
  currentRound,
  onOpenSettings,
  inputToolbarExtras,
  inputLeadingChrome,
  mode,
  onModeChange,
  execPolicy,
  onExecPolicyChange,
  inputBanner,
  onSteer,
  onFollowUp,
  pendingFollowUps,
  onRemoveFollowUp,
  onRegenerate,
  interrupted,
  onContinueInterrupted,
  onDismissInterrupted,
  finishedTasks,
  onInspectTask,
  onDismissFinishedTask,
  onShare,
  suggestedReplies,
  onSelectSuggestion,
}: LocalChatPanelProps) {
  const [inputValue, setInputValue] = useState('');
  const [files, setFiles] = useState<AttachmentFile[]>([]);
  const [mentionReferences, setMentionReferences] = useState<MentionReference[]>([]);
  const inputRef = useRef<ChatInputHandle>(null);

  const handleSelectPrompt = useCallback((prompt: string) => {
    setInputValue(prompt);
    // Wait for the textarea to receive the new value before pulling focus,
    // otherwise `focusAtEnd` would land on an empty buffer.
    requestAnimationFrame(() => {
      inputRef.current?.focusAtEnd();
    });
  }, []);

  const handleSubmit = useCallback(async () => {
    let trimmed = inputValue.trim();
    if (!trimmed && files.length === 0) return;

    // 把上传的文件持久化到会话空间：成功项用落盘路径（稳定、可被 agent
    // 读回），失败项退回原源路径。落地页（无 chatId）由调用方创建会话后
    // 自行持久化，这里 chatId 为空时原样用源路径。
    const resolvedFiles = chatId ? await saveChatAttachments(chatId, files) : files;

    // Append file references to the user message content
    if (resolvedFiles.length > 0) {
      const fileRefs = resolvedFiles.map(f => `- \`${f.path}\``).join('\n');
      if (trimmed) {
        trimmed = `${trimmed}\n\n---\n关联文件:\n${fileRefs}`;
      } else {
        trimmed = `关联文件:\n${fileRefs}`;
      }
    }

    const mentionedAgentIds = mentionReferences
      .filter((ref) => ref.type === 'agent')
      .map((ref) => ref.id);
    const referencedChatIds = mentionReferences
      .filter((ref) => ref.type === 'chat')
      .map((ref) => ref.id);
    // W6-3: image attachments ride as metadata so the main process reads the
    // bytes into base64 ImageParts; the text path refs above stay so the
    // persisted record reflects that an image was attached.
    const imageAttachments = resolvedFiles
      .filter((f) => isImageFile(f.path))
      .map((f) => ({ path: f.path, name: f.name }));
    const metadata = {
      ...(mode === 'plan' ? { mode: 'plan' as const } : {}),
      ...(execPolicy === 'full' ? { execPolicy: 'full' as const } : {}),
      ...(selectedAgentId ? { agentId: selectedAgentId } : {}),
      ...(mentionedAgentIds.length > 0 ? { mentionedAgentIds } : {}),
      ...(referencedChatIds.length > 0 ? { referencedChatIds } : {}),
      ...(imageAttachments.length > 0 ? { images: imageAttachments } : {}),
    };

    // Snapshot what we're about to clear so a failed submit can restore it —
    // clearing eagerly before `onSubmit` settles used to lose the user's text
    // (and any attachments/mentions) whenever the submit rejected.
    const submittedFiles = files;
    const submittedMentions = mentionReferences;
    setInputValue('');
    setFiles([]);
    setMentionReferences([]);
    try {
      await onSubmit({
        content: trimmed,
        metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
      });
    } catch (err) {
      // Restore so the user doesn't lose what they typed; they can retry.
      // Callers here (ChatInput) invoke this fire-and-forget (`void onSubmit()`),
      // so we swallow rather than rethrow into an unhandled rejection.
      console.warn('[LocalChatPanel] submit failed, restoring input', err);
      setInputValue(inputValue);
      setFiles(submittedFiles);
      setMentionReferences(submittedMentions);
    }
  }, [inputValue, files, mentionReferences, onSubmit, mode, execPolicy, chatId, selectedAgentId]);

  // 空会话 hero：messages 为空且未在流式时整面板切到居中首屏布局。
  const showEmptyHero = Boolean(emptyHero) && messages.length === 0 && !isStreaming;

  // Sync `chat-input-box-width` CSS var so bubbles match the input width
  // (cloud product uses 95% of the input box; we mirror that here so message
  // bubbles + input visually align in a column).
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const containerEl = containerRef.current;
    if (!containerEl) return;
    const inputBox = containerEl.querySelector<HTMLElement>('.chat-input-box');
    if (!inputBox) return;

    const sync = () => {
      const width = inputBox.getBoundingClientRect().width;
      if (width > 0) {
        containerEl.style.setProperty(
          '--chat-input-box-width',
          `${Math.round(width * 0.95)}px`,
        );
      }
    };

    sync();
    const obs = new ResizeObserver(sync);
    obs.observe(inputBox);
    return () => obs.disconnect();
    // hero ↔ 常规布局切换会重挂 ChatInput（DOM 位置变了），需要重新找
    // inputBox 并重新 observe，否则气泡宽度变量停在旧节点上。
  }, [showEmptyHero]);

  const chatInputNode = (
    <ChatInput
      ref={inputRef}
      value={inputValue}
      onChange={setInputValue}
      onSubmit={handleSubmit}
      onCancel={onCancel}
      onSteer={onSteer}
      onFollowUp={onFollowUp}
      pendingFollowUps={pendingFollowUps}
      onRemoveFollowUp={onRemoveFollowUp}
      isStreaming={isStreaming}
      disabled={disabled}
      placeholder={
        mode === 'plan'
          ? '描述你的目标，Agent 将先制定计划…'
          : inputPlaceholder
      }
      currentAgent={currentAgent}
      agents={agents}
      chats={chats}
      selectedAgentId={selectedAgentId}
      onSelectAgent={onSelectAgent}
      onOpenSettings={onOpenSettings}
      toolbarExtras={inputToolbarExtras}
      leadingChrome={inputLeadingChrome}
      mode={mode}
      onModeChange={onModeChange}
      execPolicy={execPolicy}
      onExecPolicyChange={onExecPolicyChange}
      files={files}
      onFilesChange={setFiles}
      onMentionReferencesChange={setMentionReferences}
    />
  );

  return (
    <div
      ref={containerRef}
      className={`chat-panel-container flex h-full flex-col overflow-hidden ${className ?? ''}`.trim()}
    >
      <ChatPanel.Root className="flex h-full flex-col overflow-hidden" unstyled>
        {showEmptyHero ? (
          <div className="flex min-h-0 flex-1 items-center justify-center p-3 sm:p-5">
            <div className="flex w-full max-w-2xl flex-col items-center gap-4">
              <div className="text-center">
                <h1 className="text-xl font-semibold tracking-tight text-agent-foreground">
                  {emptyHero?.title}
                </h1>
                {emptyHero?.subtitle && (
                  <p className="mt-1.5 text-xs text-agent-muted-foreground">
                    {emptyHero.subtitle}
                  </p>
                )}
              </div>
              <div className="w-full">
                {inputBanner}
                {chatInputNode}
              </div>
            </div>
          </div>
        ) : (
          <>
            {/* 不用框架的 ChatPanel.Header：它默认带 border-b 下划线且 cn 是
                纯拼接（无 tailwind-merge），类覆盖不可靠。这里用无边框的
                普通容器，padding 由内部 ChatHeader 自带。 */}
            {header ? (
              <div className="flex items-center gap-2 bg-agent-canvas">{header}</div>
            ) : null}
            <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
              <MessageList
                messages={messages}
                isStreaming={isStreaming}
                agents={agents}
                chats={chats}
                chatId={chatId}
                currentAgent={currentAgent}
                executedActionsByMessageId={executedActionsByMessageId}
                currentTurnActions={currentTurnActions}
                timelineByMessageId={timelineByMessageId}
                currentTurnTimeline={currentTurnTimeline}
                currentTurnStartedAtMs={currentTurnStartedAtMs}
                durationByMessageId={durationByMessageId}
                currentLlmSpeed={currentLlmSpeed}
                llmSpeedByMessageId={llmSpeedByMessageId}
                turnFilesByMessageId={turnFilesByMessageId}
                currentTurnFiles={currentTurnFiles}
                currentTurnChildren={currentTurnChildren}
                orchestrationChildrenByMessageId={orchestrationChildrenByMessageId}
                currentRound={currentRound}
                emptyState={emptyState ?? <EmptyChat onSelectPrompt={handleSelectPrompt} />}
                mode={mode}
                onRegenerate={onRegenerate}
                interrupted={interrupted}
                onContinueInterrupted={onContinueInterrupted}
                onDismissInterrupted={onDismissInterrupted}
                finishedTasks={finishedTasks}
                onInspectTask={onInspectTask}
                onDismissFinishedTask={onDismissFinishedTask}
                onShare={onShare}
                suggestedReplies={suggestedReplies}
                onSelectSuggestion={onSelectSuggestion}
              />
              {inputBanner}
              {chatInputNode}
            </div>
          </>
        )}
      </ChatPanel.Root>
    </div>
  );
}

export default LocalChatPanel;
