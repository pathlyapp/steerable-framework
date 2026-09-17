import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { useChatStream, type SteerOutcome } from "@steerable/agent-ui";
import type { ChatMessage, SSEEvent } from "@steerable/agent-protocol";
import { getElectronBridge, isElectron } from "@/lib/electron-bridge";
import { trackBehavior } from "@/lib/insights";
import {
  createElectronChatTransport,
  regenerateChatMessage,
} from "@/lib/chat-transport";
import { ChatHeader } from "@/components/ChatHeader";
import { LocalChatPanel } from "@/components/chat/LocalChatPanel";
import { ModelPicker } from "@/components/chat/ModelPicker";
import {
  ChatInput,
  type ChatInputHandle,
  type ChatMode,
  type MentionReference,
} from "@/components/chat/ChatInput";
import {
  persistExecPolicy,
  readStoredExecPolicy,
  type ExecPolicy,
} from "@/lib/exec-policy";
import type { ExecutedAction } from "@/components/chat/ExecutedActionsCard";
import type { ChildInfo } from "@/components/chat/OrchestrationChildrenCard";
import { foldOrchestrationChildEvents } from "@/components/chat/orchestration-children-model";
import { parseTurnBlocks, type TurnBlock } from "@/components/chat/turn-timeline";
import { parseTurnFiles, type TurnFile } from "@/components/chat/turn-files";
import { readPersistedDurationMs } from "@/components/chat/elapsed";
import { useChatTasks } from "@/components/chat/useChatTasks";
import { LuListChecks, LuArrowRight } from "react-icons/lu";
import { LocalLlmSettingsModal } from "@/components/LocalLlmSettingsModal";
import {
  ChatProjectBadge,
  ProjectPickerButton,
} from "@/components/chat/ChatProjectBadge";
import { ProjectTrustBanner } from "@/components/chat/ProjectTrustBanner";
import {
  getChatLiveStream,
  listProjects,
  type ChatLiveStream,
  type LocalChatMessage,
  type LocalProject,
  type LocalTask,
} from "@/lib/local-api";
import type { AgentOutletContext } from "@/layouts/AgentLayout";
import {
  setPendingFirstMessage,
  takePendingFirstMessage,
} from "@/lib/pending-first-message";
import { BRAND_NAME, pickDefaultAgentId } from "@/brand";

/**
 * `GET /api/v2/chats/:id/messages` returns rows in `createdAt DESC` (latest
 * first) — the endpoint's own docstring spells it out: tie-breakers are
 * arranged so that when the frontend reverses to ASC, user appears before
 * assistant within the same timestamp. The cloud frontend reverses; this
 * SPA used to forward DESC straight to the chat view, producing a visibly
 * upside-down conversation (newest at the top, oldest near the input).
 *
 * Always go through this helper before handing messages to `useChatStream`
 * — both initial hydration and the header's refresh action. New streaming
 * messages are appended by `useChatStream` itself, so they land at the end
 * (= bottom) without further work.
 */
function chronological(messages: ChatMessage[] | undefined): ChatMessage[] {
  if (!messages || messages.length === 0) return [];
  return [...messages].reverse();
}

type ChatMessageWithMetadata = ChatMessage & LocalChatMessage;

const CHAT_MODE_STORAGE_KEY = "agent-chat-mode";

function readStoredMode(): ChatMode {
  if (typeof localStorage === "undefined") return "agent";
  return localStorage.getItem(CHAT_MODE_STORAGE_KEY) === "plan"
    ? "plan"
    : "agent";
}

function extractPersistedActions(
  messages: ChatMessageWithMetadata[] | undefined,
): Record<string, ExecutedAction[]> {
  const seeded: Record<string, ExecutedAction[]> = {};
  if (!messages || messages.length === 0) return seeded;
  for (const message of messages) {
    if (message.role !== "assistant" || !message.messageMetadata) continue;
    try {
      const metadata = JSON.parse(message.messageMetadata) as {
        executedActions?: ExecutedAction[];
      };
      if (
        Array.isArray(metadata.executedActions) &&
        metadata.executedActions.length > 0
      ) {
        seeded[message.id] = metadata.executedActions;
      }
    } catch {
      // Ignore malformed legacy metadata.
    }
  }
  return seeded;
}

function extractPersistedTimelines(
  messages: ChatMessageWithMetadata[] | undefined,
): Record<string, TurnBlock[]> {
  const seeded: Record<string, TurnBlock[]> = {};
  if (!messages || messages.length === 0) return seeded;
  for (const message of messages) {
    if (message.role !== "assistant" || !message.messageMetadata) continue;
    try {
      const metadata = JSON.parse(message.messageMetadata) as {
        timeline?: unknown;
      };
      const blocks = parseTurnBlocks(metadata.timeline);
      if (blocks) seeded[message.id] = blocks;
    } catch {
      // Ignore malformed legacy metadata.
    }
  }
  return seeded;
}

function extractPersistedDurations(
  messages: ChatMessageWithMetadata[] | undefined,
): Record<string, number> {
  const seeded: Record<string, number> = {};
  if (!messages || messages.length === 0) return seeded;
  for (const message of messages) {
    if (message.role !== "assistant" || !message.messageMetadata) continue;
    const durationMs = readPersistedDurationMs(message.messageMetadata);
    if (durationMs != null) seeded[message.id] = durationMs;
  }
  return seeded;
}

function extractPersistedTurnFiles(
  messages: ChatMessageWithMetadata[] | undefined,
): Record<string, TurnFile[]> {
  const seeded: Record<string, TurnFile[]> = {};
  if (!messages || messages.length === 0) return seeded;
  for (const message of messages) {
    if (message.role !== "assistant" || !message.messageMetadata) continue;
    try {
      const metadata = JSON.parse(message.messageMetadata) as {
        turnFiles?: unknown;
      };
      const files = parseTurnFiles(metadata.turnFiles);
      if (files) seeded[message.id] = files;
    } catch {
      // Ignore malformed legacy metadata.
    }
  }
  return seeded;
}

/**
 * Routing layer for `/agent/:chatId`.
 *
 * Component split (three layers, intentional):
 *   • AgentPage         — picks empty-state vs loader based on URL params.
 *   • AgentChatLoader   — fetches initial history via IPC. Renders a loading
 *     splash until messages arrive, THEN mounts AgentChatView. We do this
 *     because `useChatStream`'s reducer only consumes `initialMessages` on
 *     its first call (the framework hook uses `useReducer((s, a) => …, {
 *     messages: initialMessages ?? [] })` — the lazy initializer runs once);
 *     mounting the view eagerly with `null → []` then trying to backfill via
 *     `setMessages` is racy and loses the user's first turn if it lands
 *     during hydration.
 *   • AgentChatView     — owns the streaming reducer + all per-turn state
 *     (round counter, executed-actions queue). Remounts on `chatId` change
 *     via `key={chatId}` so the reducer is guaranteed fresh per chat.
 *
 * Sidebar / agent picker / new-chat continue to live in `AgentSidebar`
 * (mounted by `AgentLayout`); this file only worries about the chat detail.
 */
export function AgentPage() {
  const { chatId } = useParams<{ chatId?: string }>();
  // W1.2.1: bumped on branch switch / regenerate completion — remounting
  // the loader re-hydrates the message list from the re-projected store.
  const [branchTick, setBranchTick] = useState(0);
  const bumpBranchTick = useCallback(() => setBranchTick((t) => t + 1), []);

  if (!chatId) {
    return <EmptyChatGate />;
  }

  return (
    <AgentChatLoader
      key={`${chatId}:${branchTick}`}
      chatId={chatId}
      onBranchTick={bumpBranchTick}
    />
  );
}

/**
 * AgentChatLoader hydrates initial chat history before mounting the streaming
 * view. Two states it can be in:
 *   • `initialMessages === null` → spinner; effect still in flight.
 *   • `initialMessages !== null` → forward into AgentChatView with the array
 *     (possibly empty if the chat has no prior messages or hydration failed).
 *
 * Browser-preview mode (no electron bridge) short-circuits to an empty array
 * so the view at least renders the input.
 */
function AgentChatLoader({
  chatId,
  onBranchTick,
}: {
  chatId: string;
  onBranchTick: () => void;
}) {
  const [initialMessages, setInitialMessages] = useState<ChatMessage[] | null>(
    null,
  );
  const [initialExecutedActions, setInitialExecutedActions] = useState<
    Record<string, ExecutedAction[]>
  >({});
  const [initialTimelines, setInitialTimelines] = useState<
    Record<string, TurnBlock[]>
  >({});
  const [initialDurations, setInitialDurations] = useState<
    Record<string, number>
  >({});
  const [initialTurnFiles, setInitialTurnFiles] = useState<
    Record<string, TurnFile[]>
  >({});
  // W7-1: 后端在 messages 响应里下发 interrupted（上一轮崩溃/强杀中断）。
  const [initialInterrupted, setInitialInterrupted] = useState(false);
  // 运行中回合的实时快照：切走再切回时用它在历史消息之上叠出「正在运行」
  // 的助手气泡 + 部分工具卡片 / 时间线。
  const [initialLiveStream, setInitialLiveStream] = useState<ChatLiveStream>({
    active: false,
  });
  const [hydrationError, setHydrationError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (!isElectron()) {
        if (!cancelled) setInitialMessages([]);
        return;
      }
      try {
        const bridge = getElectronBridge()!;
        const [response, live] = await Promise.all([
          bridge.localBackend.request<{
            messages?: ChatMessageWithMetadata[];
            interrupted?: boolean;
          }>({
            method: "GET",
            path: `/api/v2/chats/${encodeURIComponent(chatId)}/messages?limit=200`,
          }),
          getChatLiveStream(chatId),
        ]);
        if (cancelled) return;
        setInitialMessages(chronological(response.messages));
        setInitialExecutedActions(extractPersistedActions(response.messages));
        setInitialTimelines(extractPersistedTimelines(response.messages));
        setInitialDurations(extractPersistedDurations(response.messages));
        setInitialTurnFiles(extractPersistedTurnFiles(response.messages));
        setInitialInterrupted(response.interrupted === true);
        setInitialLiveStream(live ?? { active: false });
      } catch (err) {
        if (cancelled) return;
        setHydrationError(err instanceof Error ? err.message : String(err));
        setInitialMessages([]);
        setInitialExecutedActions({});
        setInitialTimelines({});
        setInitialDurations({});
        setInitialTurnFiles({});
        setInitialInterrupted(false);
        setInitialLiveStream({ active: false });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chatId]);

  if (initialMessages === null) {
    return (
      <div className="flex h-full w-full items-center justify-center text-sm text-agent-muted-foreground">
        加载对话历史…
      </div>
    );
  }

  return (
    <AgentChatView
      chatId={chatId}
      initialMessages={initialMessages}
      initialExecutedActions={initialExecutedActions}
      initialTimelines={initialTimelines}
      initialDurations={initialDurations}
      initialTurnFiles={initialTurnFiles}
      initialInterrupted={initialInterrupted}
      initialLiveStream={initialLiveStream}
      hydrationError={hydrationError}
      onBranchTick={onBranchTick}
    />
  );
}

interface AgentChatViewProps {
  chatId: string;
  initialMessages: ChatMessage[];
  initialExecutedActions: Record<string, ExecutedAction[]>;
  initialTimelines: Record<string, TurnBlock[]>;
  initialDurations: Record<string, number>;
  initialTurnFiles: Record<string, TurnFile[]>;
  /** W7-1: 打开会话时上一轮处于中断态（崩溃/强杀，无完成记录）。 */
  initialInterrupted: boolean;
  /** 打开会话时的运行中回合快照（切回恢复运行状态用）。 */
  initialLiveStream: ChatLiveStream;
  hydrationError: string | null;
  onBranchTick: () => void;
}

function AgentChatView({
  chatId,
  initialMessages,
  initialExecutedActions,
  initialTimelines,
  initialDurations,
  initialTurnFiles,
  initialInterrupted,
  initialLiveStream,
  hydrationError,
  onBranchTick,
}: AgentChatViewProps) {
  const ctx = useOutletContext<AgentOutletContext>();
  const chat = ctx.chats.find((c) => c.id === chatId) ?? null;
  const agent =
    (chat?.agentId ? ctx.agents.find((a) => a.id === chat.agentId) : null) ??
    null;

  // 当前会话绑定的项目 — Codex 式 cwd 指示：输入框上方显示项目名徽章，
  // 点击可关联到其他项目 / 修改项目目录 / 移出项目。无项目会话不显示。
  const [projects, setProjects] = useState<LocalProject[]>([]);
  const fetchProjects = useCallback(async () => {
    if (!isElectron()) return;
    try {
      const res = await listProjects();
      setProjects(res.projects ?? []);
    } catch {
      /* 列表失败保持旧数据 */
    }
  }, []);
  useEffect(() => {
    void fetchProjects();
  }, [fetchProjects]);
  const chatProject = chat?.projectId
    ? (projects.find((p) => p.id === chat.projectId) ?? null)
    : null;
  const transport = useMemo(
    () => createElectronChatTransport(chatId),
    [chatId],
  );

  // 4.6a 后台任务：header 角标与消息列尾部的终态卡共用这一份订阅，两处
  // 因此不会各拉一次任务表、也不会显示互相错位的状态。
  const {
    tasks,
    finished: finishedTasks,
    dismissFinished,
  } = useChatTasks(chatId);
  const handleInspectTask = useCallback(
    (task: LocalTask) => {
      ctx.inspectTask({ id: task.id, chatId: task.chatId, title: task.task });
    },
    [ctx],
  );
  // ── 运行中回合的实时快照（切走再切回时恢复运行状态）─────────────────
  // 回合不因切页而取消（见下方：卸载时不再 cancelActive），但流式 SSE 只
  // 流向发起 fetch 的那个 mount；切回后的新 mount 靠轮询 GET /live-stream
  // 把「正在运行 + 部分产出」恢复出来。initialLiveStream 是挂载时同步拿到的
  // 首帧，之后由轮询持续刷新。
  const [liveStream, setLiveStream] = useState<ChatLiveStream>(initialLiveStream);

  // ── executed_actions plumbing ────────────────────────────────────────
  // Local-backend emits `{type: 'executed_actions', actions: [...]}` AFTER each
  // tool-call round; chat-transport.ts maps it to {type: 'agent',
  // event: 'executed_actions', payload}. The framework's `useChatStream`
  // doesn't know what to do with it, so it surfaces it via `onUnknownEvent`.
  //
  // Display invariant:
  //   • While streaming, render under the *latest* assistant message via
  //     `currentTurnActions` (its framework-assigned id isn't reconciled with
  //     the DB id yet — keying by index/recency is the only option).
  //   • When the backend emits `message_id` at stream end, flush the queue
  //     into `executedActionsByMessageId[msgId]` so the card survives
  //     scrolling away / refreshing.
  //
  // History is lost on page reload because local-backend doesn't (yet) persist
  // executed_actions per message — when we hydrate from
  // `/api/v2/chats/:id/messages`, only the inline text summary survives. The
  // card surface degrades to the text summary in that case, which is still
  // readable. Phase 2c can address with a `GET /messages?include=actions`
  // expansion.
  const [executedActionsByMessageId, setExecutedActionsByMessageId] = useState<
    Record<string, ExecutedAction[]>
  >(initialExecutedActions);
  const [currentTurnActions, setCurrentTurnActions] = useState<
    ExecutedAction[]
  >([]);
  const [timelineByMessageId, setTimelineByMessageId] = useState<
    Record<string, TurnBlock[]>
  >(initialTimelines);
  const [currentTurnTimeline, setCurrentTurnTimeline] = useState<
    TurnBlock[] | undefined
  >(undefined);
  const pendingTimelineRef = useRef<TurnBlock[]>([]);
  const [durationByMessageId, setDurationByMessageId] = useState<
    Record<string, number>
  >(initialDurations);
  // 回合产物文件列表：与 executedActions 同款 reconcile 模式——live 回合
  // 先挂 pendingTurnFilesRef，`message_id` 事件到达时归档到落库 id 上；
  // 历史回合由 initialTurnFiles 从 messageMetadata.turnFiles 水合。
  const [turnFilesByMessageId, setTurnFilesByMessageId] = useState<
    Record<string, TurnFile[]>
  >(initialTurnFiles);
  // 当轮产物：turn_files 事件到达（流尾声）到 message_id 归档之间，以及
  // 归档后占位 id 消息仍在屏上的这段时间，都靠它渲染尾部消息的文件列表。
  const [currentTurnFiles, setCurrentTurnFiles] = useState<TurnFile[]>([]);
  const pendingTurnFilesRef = useRef<TurnFile[]>([]);
  const turnStartedAtRef = useRef<number | null>(null);
  const [currentTurnStartedAtMs, setCurrentTurnStartedAtMs] = useState<
    number | undefined
  >(undefined);
  const pendingDurationMessageIdRef = useRef<string | null>(null);
  // P3.1 编排：本轮子代理生命周期（orchestration_child SSE 事件累积），
  // 与 currentTurnActions 同款 reconcile 模式（message_id 落库后按键归档）。
  const [currentTurnChildren, setCurrentTurnChildren] = useState<ChildInfo[]>([]);
  const [orchestrationChildrenByMessageId, setOrchestrationChildrenByMessageId] =
    useState<Record<string, ChildInfo[]>>({});
  const pendingChildrenRef = useRef<ChildInfo[]>([]);
  // Round counter for the in-flight turn. Bumped on every `round_end` event
  // emitted by chat-transport (was previously suppressed). Driving this state
  // lets `StreamingStatus` render "Round 2 · 继续推理..." between LLM bursts —
  // before this signal, the UI looked frozen whenever the model paused to
  // wait for tool results.
  const [currentRound, setCurrentRound] = useState(1);
  // Holds the latest in-flight actions for the `message_id` reconciliation
  // — `useState`'s setter sees the latest value via the functional update
  // form, but we keep this ref so the reconciliation event handler can read
  // synchronously without race conditions across React 18's batching.
  //
  // Note: `useChatStream` keeps the current assistant message's placeholder id;
  // it does not patch it to the backend DB id when `message_id` arrives. So we
  // keep `currentTurnActions` visible until the next submit, while also storing
  // the DB-id keyed copy for history after refresh.
  const pendingActionsRef = useRef<ExecutedAction[]>([]);

  // ── Plan 模式（类 Cursor "先出计划"）────────────────────────────────────
  // mode 持久化到 localStorage，跨对话/刷新保留用户偏好。planReady 控制"开始
  // 执行计划"操作条：当一轮 plan 模式回复结束后置为 true。
  const [mode, setMode] = useState<ChatMode>(readStoredMode);
  const [execPolicy, setExecPolicy] = useState<ExecPolicy>(readStoredExecPolicy);
  const [planReady, setPlanReady] = useState(false);
  const planTurnRef = useRef(false);
  const wasStreamingRef = useRef(false);

  const handleModeChange = useCallback((next: ChatMode) => {
    setMode(next);
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(CHAT_MODE_STORAGE_KEY, next);
    }
  }, []);

  const handleExecPolicyChange = useCallback((next: ExecPolicy) => {
    setExecPolicy(next);
    persistExecPolicy(next);
  }, []);

  const handleUnknownEvent = useCallback((event: SSEEvent) => {
    const ev = event as any;
    if (ev.type !== "agent") return;
    if (ev.event === "executed_actions") {
      const actions = ev.payload?.actions as ExecutedAction[] | undefined;
      if (Array.isArray(actions)) {
        // Local-backend always emits the full accumulated list; replace, don't
        // append, to avoid double-rendering when the round re-fires.
        pendingActionsRef.current = actions;
        setCurrentTurnActions(actions);
      }
      return;
    }
    if (ev.event === "turn_timeline") {
      const blocks = parseTurnBlocks(ev.payload?.blocks);
      if (blocks) {
        pendingTimelineRef.current = blocks;
        setCurrentTurnTimeline(blocks);
      }
      return;
    }
    if (ev.event === "turn_files") {
      // 回合收尾时后端发一次（在 message_id 之前）；先挂 pending，
      // message_id 到达时随其他队列一起归档。
      const files = parseTurnFiles(ev.payload?.files);
      if (files) {
        pendingTurnFilesRef.current = files;
        setCurrentTurnFiles(files);
      }
      return;
    }
    if (ev.event === "round_end") {
      // chat-transport surfaces `completion executing` as round_end. Bumping
      // the counter here is safe even if the round emits no tools — the
      // status component only switches to "Round N" labelling once N > 1.
      // `completion cancelled`（用户 Stop / 窗口关闭）也会落到 round_end，
      // 这不是新一轮，跳过计数。
      if (ev.payload?.status === "cancelled") return;
      setCurrentRound((r) => r + 1);
      return;
    }
    if (ev.event === "orchestration_child") {
      const childId = ev.payload?.childId as string | undefined;
      const kind = ev.payload?.kind as string | undefined;
      if (!childId || !kind) return;
      const update = (list: ChildInfo[]): ChildInfo[] => {
        const idx = list.findIndex((c) => c.childId === childId);
        if (kind === "child_spawned") {
          if (idx >= 0) return list;
          return [
            ...list,
            {
              childId,
              task: ev.payload?.task as string | undefined,
              depth: ev.payload?.depth as number | undefined,
              profile: ev.payload?.profile as string | undefined,
              status: "running",
            },
          ];
        }
        if (idx < 0) return list;
        const status =
          kind === "child_completed"
            ? "completed"
            : kind === "child_failed"
              ? "failed"
              : kind === "child_cancelled"
                ? "cancelled"
                : kind === "child_interrupted"
                  ? "interrupted"
                  : kind === "child_resumed"
                    ? "running"
                    : list[idx].status;
        const next = [...list];
        next[idx] = { ...list[idx], status };
        return next;
      };
      pendingChildrenRef.current = update(pendingChildrenRef.current);
      setCurrentTurnChildren(pendingChildrenRef.current);
      return;
    }
    if (ev.event === "message_id") {
      const messageId = ev.payload?.messageId as string | undefined;
      if (!messageId) return;
      pendingDurationMessageIdRef.current = messageId;
      const queued = pendingActionsRef.current;
      if (queued.length > 0) {
        setExecutedActionsByMessageId((prev) => ({
          ...prev,
          [messageId]: queued,
        }));
      }
      pendingActionsRef.current = [];
      const queuedTimeline = pendingTimelineRef.current;
      if (queuedTimeline.length > 0) {
        setTimelineByMessageId((prev) => ({
          ...prev,
          [messageId]: queuedTimeline,
        }));
      }
      pendingTimelineRef.current = [];
      const queuedChildren = pendingChildrenRef.current;
      if (queuedChildren.length > 0) {
        setOrchestrationChildrenByMessageId((prev) => ({
          ...prev,
          [messageId]: queuedChildren,
        }));
      }
      pendingChildrenRef.current = [];
      const queuedTurnFiles = pendingTurnFilesRef.current;
      if (queuedTurnFiles.length > 0) {
        setTurnFilesByMessageId((prev) => ({
          ...prev,
          [messageId]: queuedTurnFiles,
        }));
      }
      pendingTurnFilesRef.current = [];
    }
    // AI 标题更新走的是后台 IPC 广播（chat-title-updated）而不是 SSE——
    // 见 AgentLayout 里的订阅。SSE 通道在 [DONE] 之后就不再监听了，title-gen
    // 是 fire-and-forget 的，所以这边不需要也不应该处理 chat_title_updated。
  }, []);

  const {
    messages,
    isStreaming,
    sendUserMessage,
    resumeTurn,
    steerOrFollowUpUserMessage,
    followUpUserMessage,
    pendingFollowUps,
    removeFollowUp,
    cancel,
    appendMessage,
  } = useChatStream({
    transport,
    initialMessages,
    onUnknownEvent: handleUnknownEvent,
  });

  // ── 切回恢复：远端回合仍在跑，但本 mount 不是发起者 ──────────────────
  // useChatStream.isStreaming 只在本 mount 自己发起的流时为 true；切回后的
  // 新 mount 没发起流，所以 isStreaming=false，但后端快照 liveStream.active
  // 为 true —— 此时用快照叠出「正在运行」的助手气泡 + 部分工具卡片/时间线。
  const remoteStreaming = liveStream.active === true && !isStreaming;

  // 远端轮中转向：transport.steer 按 chatId 找活跃流，天然支持「切回后注入」
  // ——不像 useChatStream.steerOrFollowUpUserMessage 那样被本地 isStreamingRef
  // 卡住。注入成功后把用户消息追加到本地 transcript（与正常转向一致）。
  const steerRemote = useCallback(
    async (text: string): Promise<SteerOutcome> => {
      const steer = transport.steer;
      const ok = steer ? await steer(text) : false;
      if (ok) {
        appendMessage({
          id: `user_steer_${Date.now()}_${Math.floor(Math.random() * 1e6)}`,
          role: "user",
          content: text,
          createdAt: new Date().toISOString(),
        });
        return "steered";
      }
      // 回合可能恰好已结束——回落 queued 让 ChatInput 给出提示，紧跟着的
      // re-hydrate 会把最终消息拉出来。
      return "queued";
    },
    [transport, appendMessage],
  );

  // 轮询运行中回合的快照，直到它结束。
  useEffect(() => {
    if (!remoteStreaming) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await getChatLiveStream(chatId);
        if (!cancelled) setLiveStream(next ?? { active: false });
      } catch {
        /* 保持上一帧快照 */
      }
    };
    void poll();
    const id = window.setInterval(poll, 750);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [chatId, remoteStreaming]);

  // 快照从 active=true → false 的边沿：远端回合结束了，re-hydrate 出最终
  // 落库消息（含真实 message id / executedActions / timeline）。
  const wasLiveActiveRef = useRef(liveStream.active === true);
  useEffect(() => {
    const nowActive = liveStream.active === true;
    if (wasLiveActiveRef.current && !nowActive) {
      onBranchTick();
    }
    wasLiveActiveRef.current = nowActive;
  }, [liveStream.active, onBranchTick]);

  // W7-1: 中断提示卡的可见性。初值来自 messages 响应的 interrupted 标记；
  // 点击「继续」/「忽略」或用户手动发起新一轮时本地清除（继续成功后后端的
  // turn_active 标记已随回复落库清除，重新加载不会再报）。
  const [interrupted, setInterrupted] = useState(initialInterrupted);

  // 监听 streaming 结束沿：若刚结束的这一轮是 plan 模式，展示"开始执行计划"操作条。
  // 同时冻结本轮墙钟，写进 durationByMessageId（占位 id + 落库 id），折叠条
  // 才能在刷新前显示「工作了 …」。
  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming) {
      if (planTurnRef.current) {
        setPlanReady(true);
        planTurnRef.current = false;
      }
      const started = turnStartedAtRef.current;
      if (started != null) {
        const elapsed = Math.max(0, Date.now() - started);
        const lastAssistant = [...messages]
          .reverse()
          .find((item) => item.role === "assistant");
        setDurationByMessageId((prev) => {
          const next = { ...prev };
          if (lastAssistant) next[lastAssistant.id] = elapsed;
          if (pendingDurationMessageIdRef.current) {
            next[pendingDurationMessageIdRef.current] = elapsed;
          }
          return next;
        });
      }
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming, messages]);

  // 真正能中断后端 agent 循环的取消：框架 hook 的 cancel 只重置前端状态
  // （cancel 句柄在流结束后才被保存，是已知的上游缺陷），这里补上
  // transport.cancelActive() 让主进程 abort LLM / 工具循环。
  const handleCancel = useCallback(() => {
    transport.cancelActive();
    cancel();
  }, [transport, cancel]);

  // Reset the in-flight queue on every new user submit. We do this in a
  // wrapper rather than directly in `handleUnknownEvent`, because there's no
  // distinct SSE event for "turn started" — `user_message` is suppressed by
  // chat-transport, and tokens just start streaming.
  // 模型选择器的每轮覆盖：null = 跟随全局设置 / 厂商预制。网关目录由
  // ModelPicker 自取（GET /api/v2/llm/models → sidecar models.list）。
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [effortOverride, setEffortOverride] = useState<string | null>(null);

  const handleSubmit = useCallback(
    (input: { content: string; metadata?: Record<string, unknown> }) => {
        trackBehavior('composer_send', {
        empty: !input.content.trim(),
        mode: input.metadata?.mode === 'plan' ? 'plan' : 'agent',
        execPolicy,
        length: input.content.trim().length,
      });
      pendingActionsRef.current = [];
      setCurrentTurnActions([]);
      pendingTimelineRef.current = [];
      setCurrentTurnTimeline([]);
      pendingChildrenRef.current = [];
      setCurrentTurnChildren([]);
      pendingTurnFilesRef.current = [];
      setCurrentTurnFiles([]);
      setCurrentRound(1);
      const started = Date.now();
      turnStartedAtRef.current = started;
      setCurrentTurnStartedAtMs(started);
      pendingDurationMessageIdRef.current = null;
      // 新一轮开始：记录本轮是否为 plan 模式，并隐藏上一份计划的操作条。
      planTurnRef.current = input.metadata?.mode === "plan";
      setPlanReady(false);
      // 手动发起新一轮即取代中断态——用户已经继续往前走了。
      setInterrupted(false);
      // 模型选择器的每轮覆盖随 metadata 下发：model 覆盖设置里的全局
      // 模型；reasoningEffort 由 sidecar 按目录严格校验（不支持即报错，
      // 不静默丢弃）。都为 null 时保持设置/预制默认。
      const metadata = {
        ...input.metadata,
        ...(modelOverride ? { model: modelOverride } : {}),
        ...(effortOverride ? { reasoningEffort: effortOverride } : {}),
        ...(execPolicy === "full" ? { execPolicy: "full" } : {}),
      };
      return sendUserMessage({
        ...input,
        metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
      });
    },
    [sendUserMessage, modelOverride, effortOverride, execPolicy],
  );

  // 包槽位 fallback 发送通道（如文档包的单页修改在 sidecar 未就绪时把
  // 后端拼好的指令退回主聊天）：AgentLayout 持有发送器 ref，这里挂载时
  // 注册、卸载时注销，包面板经 sendChatMessage 调到当前注册的 handleSubmit。
  const { registerChatMessageSender } = ctx;
  useEffect(() => {
    registerChatMessageSender(handleSubmit);
    return () => registerChatMessageSender(null);
  }, [registerChatMessageSender, handleSubmit]);

  // W7-1「继续上次回复」：走 resume 通道续跑被中断的 turn——不追加用户
  // 消息，sidecar 回放 durable record 的投影作为循环种子。每轮重置项与
  // handleSubmit 相同；plan 模式标记沿用当前模式（与正常发送一致）。
  const handleContinueInterrupted = useCallback(() => {
    if (isStreaming) return;
    setInterrupted(false);
    pendingActionsRef.current = [];
    setCurrentTurnActions([]);
    pendingTimelineRef.current = [];
    setCurrentTurnTimeline([]);
    pendingChildrenRef.current = [];
    setCurrentTurnChildren([]);
    pendingTurnFilesRef.current = [];
    setCurrentTurnFiles([]);
    setCurrentRound(1);
    const started = Date.now();
    turnStartedAtRef.current = started;
    setCurrentTurnStartedAtMs(started);
    pendingDurationMessageIdRef.current = null;
    planTurnRef.current = mode === "plan";
    setPlanReady(false);
    const metadata = {
      ...(mode === "plan" ? { mode: "plan" as const } : {}),
      ...(execPolicy === "full" ? { execPolicy: "full" as const } : {}),
    };
    return resumeTurn({
      metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
    });
  }, [isStreaming, mode, execPolicy, resumeTurn]);

  // W7-1「忽略」：仅本次挂载隐藏卡片，不写库——标记仍在，下次打开会再提示。
  const handleDismissInterrupted = useCallback(() => {
    setInterrupted(false);
  }, []);

  // 轮中转向 + 失败兜底（W6-2）：streaming 期间 Enter 优先注入运行中的
  // CoreLoop 回合；注入不了时 hook 按回合实况降级——仍在跑则排入待发队列
  // （'queued'，ChatInput 提示"已改为排队"），恰好已结束则作为新回合直发
  // （'sent'）。消息不会丢，ChatInput 对任何结果都清草稿。
  const handleSteer = useCallback(
    (text: string) =>
      remoteStreaming ? steerRemote(text) : steerOrFollowUpUserMessage(text),
    [remoteStreaming, steerRemote, steerOrFollowUpUserMessage],
  );

  // follow-up 排队（W6-2）：streaming 期间 ⌘/Ctrl+Enter 把文本排入待发
  // 队列，本轮结束后自动作为下一轮发出。与转向（注入当前回合）相对。
  // 切回后的远端回合没有本地 follow-up 队列可挂，降级为注入当前回合。
  const handleFollowUp = useCallback(
    (text: string) => {
      if (remoteStreaming) {
        void steerRemote(text);
        return;
      }
      followUpUserMessage({ content: text });
    },
    [remoteStreaming, steerRemote, followUpUserMessage],
  );

  // 首页输入框接力：EmptyChatGate 建好 chat 并跳转后，首条消息暂存在
  // pending-first-message 里，这里挂载后自动发出——用户感知就是"首页输入
  // 即开聊"。take 取出即清 + ref 守卫，StrictMode 双跑 effect 不会重发。
  const pendingFirstSentRef = useRef(false);
  useEffect(() => {
    if (pendingFirstSentRef.current) return;
    const pending = takePendingFirstMessage(chatId);
    if (!pending) return;
    pendingFirstSentRef.current = true;
    const pendingModel =
      typeof pending.metadata?.model === "string" ? pending.metadata.model : null;
    const pendingEffort =
      typeof pending.metadata?.reasoningEffort === "string"
        ? pending.metadata.reasoningEffort
        : null;
    if (pendingModel) setModelOverride(pendingModel);
    if (pendingEffort) setEffortOverride(pendingEffort);
    void handleSubmit({
      content: pending.content,
      metadata: pending.metadata,
    });
  }, [chatId, handleSubmit]);

  // "开始执行计划"：切回 agent 模式并自动发送执行指令，恢复全量工具。
  const handleExecutePlan = useCallback(() => {
    handleModeChange("agent");
    setPlanReady(false);
    void handleSubmit({ content: "请按照上面的计划开始执行。" });
  }, [handleModeChange, handleSubmit]);

  // 分享当前对话：截取整个聊天面板区域（含 header + 消息 + 输入框），
  // 由主进程 capturePage 后写入系统剪贴板，用户可直接粘贴到任何地方。
  const handleShare = useCallback(async (): Promise<boolean> => {
    if (!isElectron()) return false;
    const bridge = getElectronBridge()!;
    const panel = document.querySelector('.chat-panel-container');
    const rect = panel?.getBoundingClientRect();
    const result = await bridge.local?.captureScreenshot(
      rect && rect.width > 0 && rect.height > 0
        ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        : undefined,
    );
    return result?.success === true;
  }, []);

  // ChatInput toolbar's gear button — opens the same LLM settings modal the
  // sidebar's bottom button opens. Two independent mounts is fine: they read
  // the same backend config endpoint and the user can only see one at a time.
  // Lifting modal state to AgentLayout would let us mount once but adds
  // outlet-context plumbing that isn't worth it for a binary on/off flag.
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);

  const handleSelectAgent = useCallback(
    (agentId: string) => {
      ctx.setSelectedAgentId(agentId);
    },
    [ctx],
  );

  // ── 把远端运行中的快照叠进渲染层 ────────────────────────────────────
  const remoteContent = remoteStreaming ? (liveStream.content ?? "") : null;
  const effectiveMessages = useMemo<ChatMessage[]>(() => {
    if (remoteContent === null) return messages;
    return [
      ...messages,
      {
        id: `live_${chatId}`,
        role: "assistant",
        content: remoteContent,
        createdAt: new Date().toISOString(),
      },
    ];
  }, [messages, remoteContent, chatId]);
  const effectiveIsStreaming = isStreaming || remoteStreaming;
  const effectiveCurrentTurnActions: ExecutedAction[] = remoteStreaming
    ? ((liveStream.executedActions as ExecutedAction[] | undefined) ?? [])
    : currentTurnActions;
  const effectiveCurrentTurnTimeline: TurnBlock[] | undefined = remoteStreaming
    ? (parseTurnBlocks(liveStream.timeline) ?? undefined)
    : currentTurnTimeline;
  const effectiveCurrentTurnChildren: ChildInfo[] = remoteStreaming
    ? foldOrchestrationChildEvents(
        (liveStream.children ?? []) as ReadonlyArray<Record<string, unknown>>,
      )
    : currentTurnChildren;

  return (
    <div className="flex h-full w-full flex-col">
      {hydrationError && (
        <div className="border-b border-agent-border bg-agent-muted/60 px-3 py-1 text-xs text-agent-destructive">
          加载对话历史失败：{hydrationError}
        </div>
      )}
      <LocalChatPanel
        messages={effectiveMessages}
        isStreaming={effectiveIsStreaming}
        onSubmit={handleSubmit}
        onCancel={handleCancel}
        onSteer={handleSteer}
        onFollowUp={handleFollowUp}
        pendingFollowUps={pendingFollowUps.map((m) => m.content)}
        onRemoveFollowUp={removeFollowUp}
        className="flex-1"
        emptyHero={{
          title: BRAND_NAME,
          subtitle: '输入消息，直接开始一段新对话。',
        }}
        header={
          <ChatHeader
            chat={chat}
            agent={agent}
            onBranchSwitched={isElectron() ? onBranchTick : undefined}
            onInspectTask={ctx.inspectTask}
            tasks={tasks}
          />
        }
        onRegenerate={
          isElectron()
            ? async (messageId) => {
                // 后端 regenerate 流跑完后桌面 store 已是新分支投影；
                // bump tick 重挂消息列表。
                await regenerateChatMessage(chatId, messageId);
                onBranchTick();
              }
            : undefined
        }
        inputPlaceholder="向本地 Agent 发送消息…"
        agents={ctx.agents}
        chats={ctx.chats}
        currentAgent={agent}
        selectedAgentId={ctx.selectedAgentId}
        onSelectAgent={handleSelectAgent}
        executedActionsByMessageId={executedActionsByMessageId}
        currentTurnActions={effectiveCurrentTurnActions}
        timelineByMessageId={timelineByMessageId}
        currentTurnTimeline={effectiveCurrentTurnTimeline}
        currentTurnStartedAtMs={currentTurnStartedAtMs}
        durationByMessageId={durationByMessageId}
        turnFilesByMessageId={turnFilesByMessageId}
        currentTurnFiles={currentTurnFiles}
        currentTurnChildren={effectiveCurrentTurnChildren}
        orchestrationChildrenByMessageId={orchestrationChildrenByMessageId}
        currentRound={currentRound}
        interrupted={interrupted}
        onContinueInterrupted={handleContinueInterrupted}
        onDismissInterrupted={handleDismissInterrupted}
        finishedTasks={finishedTasks}
        onInspectTask={handleInspectTask}
        onDismissFinishedTask={dismissFinished}
        onShare={isElectron() ? handleShare : undefined}
        onOpenSettings={
          isElectron() ? () => setLlmSettingsOpen(true) : undefined
        }
        inputToolbarExtras={
          <ModelPicker
            model={modelOverride}
            reasoningEffort={effortOverride}
            onSelectModel={setModelOverride}
            onSelectEffort={setEffortOverride}
            disabled={isStreaming}
          />
        }
        mode={mode}
        onModeChange={handleModeChange}
        execPolicy={execPolicy}
        onExecPolicyChange={handleExecPolicyChange}
        inputLeadingChrome={
          <ChatProjectBadge
            chatId={chatId}
            project={chatProject}
            projects={projects}
            onProjectsChanged={fetchProjects}
            onChatProjectChanged={ctx.refreshChats}
          />
        }
        inputBanner={
          <>
            {/* W6-5 项目信任门控：项目含规则文件但未信任时提示授权。 */}
            <ProjectTrustBanner
              chatId={chatId}
              project={chatProject}
              onTrustChanged={fetchProjects}
            />
            {planReady && !isStreaming ? (
            <div className="mx-3 mb-1 flex items-center justify-between gap-3 rounded-agent-md border border-amber-400/50 bg-amber-400/10 px-3 py-2 text-xs">
              <div className="flex items-center gap-2 text-amber-700 dark:text-amber-300">
                <LuListChecks className="h-4 w-4 shrink-0" />
                <span>计划已生成。确认无误后可切换到 Agent 模式开始执行。</span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPlanReady(false)}
                  className="rounded-full px-2 py-1 text-agent-muted-foreground transition-colors hover:text-agent-foreground"
                  data-testid="plan-dismiss"
                >
                  忽略
                </button>
                <button
                  type="button"
                  onClick={handleExecutePlan}
                  className="inline-flex items-center gap-1 rounded-full bg-agent-foreground px-3 py-1 font-medium text-agent-canvas transition hover:opacity-90"
                  data-testid="plan-execute"
                >
                  开始执行计划
                  <LuArrowRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            ) : null}
          </>
        }
      />
      {isElectron() && (
        <LocalLlmSettingsModal
          open={llmSettingsOpen}
          onClose={() => setLlmSettingsOpen(false)}
        />
      )}
      {/* W4-1 审批弹窗挂在 AgentLayout（回合不因页面切换而暂停，
          模态必须跨路由可渲染）。 */}
    </div>
  );
}

/**
 * 无 chatId 的落地页 —— workbuddy 式首页：居中一个完整的 ChatInput，
 * 输入即开聊。提交时 createChat → 暂存首条消息 → navigate 到新对话，
 * AgentChatView 挂载后自动把这条消息发出去（见 pending-first-message）。
 * 侧栏「新对话」只打开本页，不预先落库；没有发出去的内容不会出现在会话列表。
 *
 * ChatInput 是纯 props 驱动组件（无 ChatPanel context 依赖），这里独立
 * 渲染一份，agent 选择 / 模型选择 / plan 模式 / @ 引用 / 文件附加全部
 * 可用——与对话内输入框能力对齐。agent 选择只改 selectedAgentId（还没有
 * chat 可绑）；模型覆盖随 pending-first-message metadata 交给新对话。
 */
function EmptyChatGate() {
  const ctx = useOutletContext<AgentOutletContext>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const projectIdFromUrl = searchParams.get("projectId");
  const [inputValue, setInputValue] = useState("");
  const [files, setFiles] = useState<{ name: string; path: string }[]>([]);
  const [mentionReferences, setMentionReferences] = useState<
    MentionReference[]
  >([]);
  const [mode, setMode] = useState<ChatMode>(readStoredMode);
  const [execPolicy, setExecPolicy] = useState<ExecPolicy>(readStoredExecPolicy);
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [effortOverride, setEffortOverride] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [llmSettingsOpen, setLlmSettingsOpen] = useState(false);
  const inputRef = useRef<ChatInputHandle>(null);

  // 落地页项目选择：还没有 chat 可绑，选中的 projectId 在首次提交
  // createChat 时一并传入。侧栏项目组「+」会带 ?projectId= 预选。
  const [projects, setProjects] = useState<LocalProject[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    projectIdFromUrl,
  );
  useEffect(() => {
    setSelectedProjectId(projectIdFromUrl);
  }, [projectIdFromUrl]);
  useEffect(() => {
    if (!isElectron()) return;
    let cancelled = false;
    listProjects()
      .then((res) => {
        if (!cancelled) setProjects(res.projects ?? []);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    inputRef.current?.focusAtEnd();
  }, []);

  const selectedAgent =
    ctx.agents.find((a) => a.id === ctx.selectedAgentId) ??
    ctx.agents.find((a) => a.id === pickDefaultAgentId(ctx.agents)) ??
    ctx.agents[0] ??
    null;

  const handleModeChange = useCallback((next: ChatMode) => {
    setMode(next);
    if (typeof localStorage !== "undefined") {
      localStorage.setItem(CHAT_MODE_STORAGE_KEY, next);
    }
  }, []);

  const handleExecPolicyChange = useCallback((next: ExecPolicy) => {
    setExecPolicy(next);
    persistExecPolicy(next);
  }, []);

  const handleSelectAgent = useCallback(
    (agentId: string) => {
      ctx.setSelectedAgentId(agentId);
    },
    [ctx],
  );

  const handleSubmit = useCallback(async () => {
    if (isCreating) return;
    let trimmed = inputValue.trim();
    if (!trimmed && files.length === 0) return;
    trackBehavior('composer_send', {
      empty: false,
      home: true,
      mode: mode === 'plan' ? 'plan' : 'agent',
      execPolicy,
      length: trimmed.length,
    });

    // 与 LocalChatPanel.handleSubmit 相同的装配规则：文件路径附到正文，
    // @ 引用进 metadata，plan 模式打 metadata.mode。
    if (files.length > 0) {
      const fileRefs = files.map((f) => `- \`${f.path}\``).join("\n");
      trimmed = trimmed
        ? `${trimmed}\n\n---\n关联文件:\n${fileRefs}`
        : `关联文件:\n${fileRefs}`;
    }
    const mentionedAgentIds = mentionReferences
      .filter((ref) => ref.type === "agent")
      .map((ref) => ref.id);
    const referencedChatIds = mentionReferences
      .filter((ref) => ref.type === "chat")
      .map((ref) => ref.id);
    const metadata = {
      ...(mode === "plan" ? { mode: "plan" as const } : {}),
      ...(execPolicy === "full" ? { execPolicy: "full" as const } : {}),
      ...(ctx.selectedAgentId ? { agentId: ctx.selectedAgentId } : {}),
      ...(mentionedAgentIds.length > 0 ? { mentionedAgentIds } : {}),
      ...(referencedChatIds.length > 0 ? { referencedChatIds } : {}),
      ...(modelOverride ? { model: modelOverride } : {}),
      ...(effortOverride ? { reasoningEffort: effortOverride } : {}),
    };

    setIsCreating(true);
    setCreateError(null);
    try {
      const id = await ctx.createChat({
        ...(selectedProjectId ? { projectId: selectedProjectId } : {}),
        ...(ctx.selectedAgentId ? { agentId: ctx.selectedAgentId } : {}),
      });
      if (!id) throw new Error("创建对话失败，请重试");
      setPendingFirstMessage({
        chatId: id,
        content: trimmed,
        metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
      });
      navigate(`/agent/${id}`);
      // 跳转成功后组件即卸载，不用清 isCreating；失败才恢复可交互。
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
      setIsCreating(false);
    }
  }, [inputValue, files, mentionReferences, mode, execPolicy, modelOverride, effortOverride, isCreating, selectedProjectId, ctx, navigate]);

  return (
    <div
      className="flex h-full w-full items-center justify-center p-4 sm:p-8"
      data-testid="empty-chat-home"
    >
      <div className="flex w-full max-w-2xl flex-col items-center gap-6">
        <div className="text-center">
          <h1 className="text-xl font-semibold tracking-tight text-agent-foreground">
            {BRAND_NAME}
          </h1>
          <p className="mt-1.5 text-sm text-agent-muted-foreground">
            输入消息，直接开始一段新对话。
          </p>
        </div>
        <div className="w-full">
          <ChatInput
            ref={inputRef}
            value={inputValue}
            onChange={setInputValue}
            onSubmit={handleSubmit}
            disabled={!isElectron() || isCreating}
            placeholder={
              mode === "plan"
                ? "描述你的目标，Agent 将先制定计划…"
                : "向本地 Agent 发送消息…"
            }
            currentAgent={selectedAgent}
            agents={ctx.agents}
            chats={ctx.chats}
            selectedAgentId={ctx.selectedAgentId}
            onSelectAgent={handleSelectAgent}
            onOpenSettings={
              isElectron() ? () => setLlmSettingsOpen(true) : undefined
            }
            toolbarExtras={
              <ModelPicker
                model={modelOverride}
                reasoningEffort={effortOverride}
                onSelectModel={setModelOverride}
                onSelectEffort={setEffortOverride}
                disabled={!isElectron() || isCreating}
              />
            }
            leadingChrome={
              isElectron() ? (
                <ProjectPickerButton
                  projects={projects}
                  value={selectedProjectId}
                  onChange={setSelectedProjectId}
                />
              ) : undefined
            }
            mode={mode}
            onModeChange={handleModeChange}
            execPolicy={execPolicy}
            onExecPolicyChange={handleExecPolicyChange}
            files={files}
            onFilesChange={setFiles}
            onMentionReferencesChange={setMentionReferences}
          />
        </div>
        {isCreating && (
          <p className="text-xs text-agent-muted-foreground">
            正在创建对话…
          </p>
        )}
        {createError && (
          <p className="text-xs text-agent-destructive" role="alert">
            {createError}
          </p>
        )}
        {!isElectron() && (
          <p className="text-xs text-agent-destructive">
            浏览器预览模式 — 没有 Electron IPC 桥接，聊天列表与流式响应不可用。
          </p>
        )}
      </div>
      {isElectron() && (
        <LocalLlmSettingsModal
          open={llmSettingsOpen}
          onClose={() => setLlmSettingsOpen(false)}
        />
      )}
    </div>
  );
}
