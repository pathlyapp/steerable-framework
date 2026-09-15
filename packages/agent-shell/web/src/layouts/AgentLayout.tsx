import { useCallback, useEffect, useRef, useState } from 'react';
import { Outlet, useParams } from 'react-router-dom';
import { LuPanelLeftOpen } from 'react-icons/lu';
import { AgentSidebar } from '@/components/AgentSidebar';
import { TerminalPanel } from '@/components/TerminalPanel';
import { TaskProcessPanel, type InspectedTask } from '@/components/chat/TaskProcessPanel';
import { ApprovalModalHost } from '@/components/chat/ApprovalModal';
import { AskUserModalHost } from '@/components/chat/AskUserModalHost';
import { InsightsConsentBanner } from '@/components/settings/InsightsSettingsPanel';
import { trackBehavior } from '@/lib/insights';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';
import { deleteChatIfEmpty, pruneEmptyChats } from '@/lib/local-api';
import { getPackChatSlots } from '@/packs/registry';
import {
  useChatsAndAgents,
  type UseChatsAndAgentsResult,
} from '@/hooks/useChatsAndAgents';

/**
 * AgentLayout wraps `/agent`, `/agent/:chatId`, and `/` (default) with a
 * **resizable** sidebar + main content area. Pack-registered debug viewers
 * lives OUTSIDE this layout in main.tsx, mounted directly under AppShell
 * (it's loaded by its own dedicated native window).
 *
 * The chat/agents data lives at this layout level so the sidebar and any
 * child route (chat header, future chat info panel) all share one cache.
 * Children read it via `useOutletContext<AgentOutletContext>()`.
 *
 * Terminal panel:
 *   - Codex-style integrated terminal — a toggleable column BESIDE the
 *     chat content (not a route, not a separate window), so the xterm
 *     view stays mounted while the user switches chats. Toggled from the
 *     sidebar 终端 button or Cmd+T (`menu:open-terminal`, subscribed in
 *     AgentSidebar). Open state + width persist to localStorage.
 *   - Auto-reveal: when the agent runs a shell command through the
 *     visible PTY, main broadcasts `terminal:reveal` and this layout
 *     opens the panel (idempotent) so the user watches the agent type.
 *   - Closing the panel only unmounts the xterm VIEW; the PTY session is
 *     a main-process singleton and keeps running. Reopening replays the
 *     output buffer via `terminal:ensure`.
 *
 * Sidebar width:
 *   - Drag the handle (between sidebar + content) to resize.
 *   - Double-click the handle to reset to DEFAULT_SIDEBAR_WIDTH.
 *   - Width is persisted to localStorage so each user has a stable layout
 *     across sessions. Bounded by [MIN_SIDEBAR_WIDTH, MAX_SIDEBAR_WIDTH].
 *   - Body cursor + user-select are locked during drag to prevent text
 *     selection flicker while the user is dragging across chat rows.
 *   - The sidebar can be COLLAPSED entirely (header button in
 *     AgentSidebar) — a slim rail with an expand button takes its place.
 *     Collapse state is persisted too (`SIDEBAR_COLLAPSED_KEY`).
 *
 * Matches `deeppath/apps/web/src/app/agent/page.tsx`'s old resize ergonomics
 * verbatim. Width key intentionally namespaced under `deeppath.agent.*`
 * so it stays the same value the cloud sibling persists — users hopping
 * between the two won't lose their preferred width.
 */
const SIDEBAR_WIDTH_KEY = 'deeppath.agent.sidebarWidth';
const SIDEBAR_COLLAPSED_KEY = 'deeppath.agent.sidebarCollapsed';
const DEFAULT_SIDEBAR_WIDTH = 200;
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 420;

const TERMINAL_OPEN_KEY = 'deeppath.agent.terminalOpen';
const RIGHT_PANEL_KEY = 'deeppath.agent.rightPanel';
const TERMINAL_WIDTH_KEY = 'deeppath.agent.terminalWidth';
const DEFAULT_TERMINAL_WIDTH = 520;
const MIN_TERMINAL_WIDTH = 360;
const MAX_TERMINAL_WIDTH = 960;

/**
 * 右侧栏位状态：null = 都不打开，'terminal' = 终端，其余字符串 = 包注册的
 * 聊天页槽位（chatSlots，如文档包的预览栏位）。栏位同一时刻只开一个
 * （互斥规则在本布局，不在包）。
 */
export type RightPanelState = string | null;

/** 从聊天页外把一段内容作为普通用户消息发进当前会话（包槽位的 fallback 通道）。 */
export type ChatMessageSender = (input: {
  content: string;
  metadata?: Record<string, unknown>;
}) => boolean | void | Promise<boolean | void>;

export type AgentOutletContext = UseChatsAndAgentsResult & {
  /** AgentPage 挂载时注册它的 handleSubmit，供包槽位面板 fallback 发送。 */
  registerChatMessageSender: (fn: ChatMessageSender | null) => void;
  /** 发送普通用户消息到当前聊天；未注册时返回 false。 */
  sendChatMessage: ChatMessageSender;
  inspectTask: (task: InspectedTask) => void;
};

export function AgentLayout() {
  const data = useChatsAndAgents();
  const { chatId } = useParams<{ chatId?: string }>();

  // ── 包槽位 fallback 发送通道：AgentPage 注册 handleSubmit，包面板（如
  // PPT 单页修改在 sidecar 未就绪时）经 sendChatMessage 退回主聊天发送。
  const chatMessageSenderRef = useRef<ChatMessageSender | null>(null);
  const registerChatMessageSender = useCallback((fn: ChatMessageSender | null) => {
    chatMessageSenderRef.current = fn;
  }, []);
  const sendChatMessage = useCallback<ChatMessageSender>((input) => {
    const fn = chatMessageSenderRef.current;
    if (!fn) return false;
    return fn(input);
  }, []);

  // 订阅后台 AI 标题异步更新。Backend 在 chat 第一条回复完成后 fire-and-forget
  // 跑 LLM 生成标题，完成时通过 IPC 广播 `chat-title-updated`。这里就近 patch
  // sidebar 的标题——不打后端、不 refresh 列表（避免重置分页）。
  //
  // 放在 layout 而不是 AgentPage：因为 AgentPage 只在打开某个 chat 时挂载，
  // 标题更新可能在用户已经切到别的 chat 时才来；layout 一直在，订阅一次就够。
  // 用 ref 把 patchChatTitle 包起来——这样 effect 只跑一次（只依赖空数组）
  // 而 callback 永远拿到最新版本。
  const patchChatTitleRef = useRef(data.patchChatTitle);
  useEffect(() => {
    patchChatTitleRef.current = data.patchChatTitle;
  }, [data.patchChatTitle]);
  const refreshChatsRef = useRef(data.refreshChats);
  useEffect(() => {
    refreshChatsRef.current = data.refreshChats;
  }, [data.refreshChats]);
  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.onChatTitleUpdated) return;
    const unsubscribe = bridge.onChatTitleUpdated((payload) => {
      patchChatTitleRef.current(payload.chatId, payload.title);
    });
    return unsubscribe;
  }, []);
  // 会话补建通知：Backend 对本地不存在的 chatId 首次发送时会按 URL 里的 id
  // 现场补建（见 router.handleStream），随后广播 `chat-created`。这里刷一次
  // 侧栏把这条会话拉出来——否则会出现"URL 能聊、列表里却查无此会话"。
  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.onChatCreated) return;
    const unsubscribe = bridge.onChatCreated(() => {
      void refreshChatsRef.current();
    });
    return unsubscribe;
  }, []);

  // 空会话不进侧栏：启动时清掉从未发过消息的残骸；离开一段空对话时丢掉它。
  // except / 离开检测都避开当前打开的 chatId，避免和首页首条发送抢跑。
  useEffect(() => {
    if (!isElectron()) return;
    let cancelled = false;
    void pruneEmptyChats(chatId)
      .then((res) => {
        if (cancelled || (res.deletedChatIds?.length ?? 0) === 0) return;
        return refreshChatsRef.current();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // 只在 layout 挂载时扫一次历史空会话。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const prevChatIdRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    const prev = prevChatIdRef.current;
    prevChatIdRef.current = chatId;
    if (!isElectron() || !prev || prev === chatId) return;
    void deleteChatIfEmpty(prev)
      .then((res) => {
        if (res.deleted) return refreshChatsRef.current();
      })
      .catch(() => {});
  }, [chatId]);

  const [sidebarWidth, setSidebarWidth] = useState<number>(
    DEFAULT_SIDEBAR_WIDTH,
  );
  // 收起状态持久化——刷新/重启后保持用户偏好的布局。收起时渲染一条窄
  // rail（只有展开按钮），而不是把侧边栏整个从树里摘掉。
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';
    } catch {
      return false;
    }
  });
  const toggleSidebarCollapsed = useCallback(() => {
    setSidebarCollapsed((v) => {
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, v ? '0' : '1');
      } catch {
        /* ignore */
      }
      return !v;
    });
  }, []);
  const [isResizing, setIsResizing] = useState(false);
  // We keep the live width in a ref so the mousemove handler always reads
  // the freshest value (the `mouseup` cleanup needs to persist the last
  // width, but it captures `sidebarWidth` from the closure — without a ref
  // it would lag one frame behind on fast drags).
  const sidebarWidthRef = useRef<number>(DEFAULT_SIDEBAR_WIDTH);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(SIDEBAR_WIDTH_KEY);
      if (saved) {
        const n = parseInt(saved, 10);
        if (
          !Number.isNaN(n) &&
          n >= MIN_SIDEBAR_WIDTH &&
          n <= MAX_SIDEBAR_WIDTH
        ) {
          setSidebarWidth(n);
          sidebarWidthRef.current = n;
        }
      }
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    if (!isResizing) return;
    const onMove = (e: MouseEvent) => {
      const next = Math.min(
        MAX_SIDEBAR_WIDTH,
        Math.max(MIN_SIDEBAR_WIDTH, e.clientX),
      );
      sidebarWidthRef.current = next;
      setSidebarWidth(next);
    };
    const onUp = () => {
      setIsResizing(false);
      try {
        window.localStorage.setItem(
          SIDEBAR_WIDTH_KEY,
          String(sidebarWidthRef.current),
        );
      } catch {
        /* ignore */
      }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizing]);

  const resetWidth = useCallback(() => {
    setSidebarWidth(DEFAULT_SIDEBAR_WIDTH);
    sidebarWidthRef.current = DEFAULT_SIDEBAR_WIDTH;
    try {
      window.localStorage.setItem(
        SIDEBAR_WIDTH_KEY,
        String(DEFAULT_SIDEBAR_WIDTH),
      );
    } catch {
      /* ignore */
    }
  }, []);

  // ───── Right-side panel (embedded column beside the chat content) ─────
  // 终端与包槽位面板（如 PPT 预览）合并为同一栏位的互斥切换：
  // null | 'terminal' | <slotId>。持久化到新 key；老版本只持久化终端打开
  // 状态，首次读取时迁移。
  const packChatSlots = getPackChatSlots();
  const [rightPanel, setRightPanelState] = useState<RightPanelState>(() => {
    try {
      const saved = window.localStorage.getItem(RIGHT_PANEL_KEY);
      if (saved !== null) {
        return saved === 'terminal' || packChatSlots.some((s) => s.slotId === saved)
          ? saved
          : null;
      }
      // 新 key 不存在时才迁移老版本仅持久化终端的旧 key。
      return window.localStorage.getItem(TERMINAL_OPEN_KEY) === '1'
        ? 'terminal'
        : null;
    } catch {
      return null;
    }
  });

  // 自动展开 / 手动切换回调是挂载时注册的，直接读 state 会拿到过期闭包，
  // 所以用 ref 跟踪当前栏位状态（见下方同步 effect）。
  const rightPanelRef = useRef(rightPanel);

  const persistRightPanel = useCallback((next: RightPanelState) => {
    try {
      window.localStorage.setItem(RIGHT_PANEL_KEY, next ?? '');
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    rightPanelRef.current = rightPanel;
  }, [rightPanel]);

  const closeRightPanel = useCallback(() => {
    rightPanelRef.current = null;
    setRightPanelState(null);
    persistRightPanel(null);
  }, [persistRightPanel]);

  /** 分段控件 / 快捷键入口：点已打开的按钮关闭；点另一个按钮直接切换。
      打开任一面板即离开任务 dock（栏位同一时刻只呈现一个内容）。 */
  const toggleRightPanel = useCallback(
    (kind: string) => {
      const next: RightPanelState = rightPanelRef.current === kind ? null : kind;
      rightPanelRef.current = next;
      if (next !== null) setInspectedTask(null);
      setRightPanelState(next);
      persistRightPanel(next);
    },
    [persistRightPanel],
  );
  // dock 里当前显示的任务（null = 显示终端）。recentTask 在切回终端后仍
  // 保留，终端头部的「后台任务」按钮据此切回来——任务的挑选入口只有一个，
  // 就是 chat 头部的后台任务弹层。
  const [inspectedTask, setInspectedTask] = useState<InspectedTask | null>(null);
  const [recentTask, setRecentTask] = useState<InspectedTask | null>(null);
  const [terminalWidth, setTerminalWidth] = useState<number>(
    DEFAULT_TERMINAL_WIDTH,
  );
  const [isTerminalResizing, setIsTerminalResizing] = useState(false);
  const terminalWidthRef = useRef<number>(DEFAULT_TERMINAL_WIDTH);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(TERMINAL_WIDTH_KEY);
      if (saved) {
        const n = parseInt(saved, 10);
        if (
          !Number.isNaN(n) &&
          n >= MIN_TERMINAL_WIDTH &&
          n <= MAX_TERMINAL_WIDTH
        ) {
          setTerminalWidth(n);
          terminalWidthRef.current = n;
        }
      }
    } catch {
      /* ignore */
    }
  }, []);

  const inspectTask = useCallback((task: InspectedTask) => {
    setInspectedTask(task);
    setRecentTask(task);
  }, []);

  const showRecentTask = useCallback(() => {
    setInspectedTask(recentTask);
  }, [recentTask]);

  const closeTaskProcess = useCallback(() => {
    setInspectedTask(null);
  }, []);

  const showTerminalFromTask = useCallback(() => {
    setInspectedTask(null);
    rightPanelRef.current = 'terminal';
    setRightPanelState('terminal');
    persistRightPanel('terminal');
  }, [persistRightPanel]);

  // Codex 式 auto-reveal：agent 的命令进入可见 PTY 时，main 广播
  // `terminal:reveal`，这里自动展开终端面板。幂等——已打开时是 no-op；
  // 用户手动关掉后，下一条命令会再次拉开（符合"看 agent 打字"的意图）。
  useEffect(() => {
    const bridge = getElectronBridge();
    if (!bridge?.terminal?.onReveal) return;
    return bridge.terminal.onReveal(() => {
      setInspectedTask(null);
      rightPanelRef.current = 'terminal';
      setRightPanelState('terminal');
      persistRightPanel('terminal');
    });
  }, [persistRightPanel]);

  // 包槽位的自动展开（如文档包：后端在本轮产出新设计稿时广播包事件）。
  // 互斥规则：仅当栏位空闲（null）时 reveal 才生效；
  // 已打开任一面板时，自动展开请求被忽略。订阅逻辑由包自己实现。
  useEffect(() => {
    const cleanups: Array<() => void> = [];
    for (const slot of packChatSlots) {
      const cleanup = slot.setupAutoReveal?.({
        reveal: () => {
          if (rightPanelRef.current !== null) return;
          rightPanelRef.current = slot.slotId;
          setRightPanelState(slot.slotId);
          persistRightPanel(slot.slotId);
        },
        getCurrentChatId: () => chatId ?? null,
      });
      if (cleanup) cleanups.push(cleanup);
    }
    return () => {
      for (const cleanup of cleanups) cleanup();
    };
    // packChatSlots 在 bootstrap 后稳定；chatId 经 getCurrentChatId 闭包读取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, persistRightPanel]);

  // Same drag ergonomics as the sidebar handle, mirrored: the terminal's
  // right edge is pinned to the window's right padding (p-2 = 8px), so the
  // width is the distance from the cursor to that edge.
  useEffect(() => {
    if (!isTerminalResizing) return;
    const onMove = (e: MouseEvent) => {
      const next = Math.min(
        MAX_TERMINAL_WIDTH,
        Math.max(MIN_TERMINAL_WIDTH, window.innerWidth - 8 - e.clientX),
      );
      terminalWidthRef.current = next;
      setTerminalWidth(next);
    };
    const onUp = () => {
      setIsTerminalResizing(false);
      try {
        window.localStorage.setItem(
          TERMINAL_WIDTH_KEY,
          String(terminalWidthRef.current),
        );
      } catch {
        /* ignore */
      }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isTerminalResizing]);

  const resetTerminalWidth = useCallback(() => {
    setTerminalWidth(DEFAULT_TERMINAL_WIDTH);
    terminalWidthRef.current = DEFAULT_TERMINAL_WIDTH;
    try {
      window.localStorage.setItem(
        TERMINAL_WIDTH_KEY,
        String(DEFAULT_TERMINAL_WIDTH),
      );
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    trackBehavior('app_open', { path: window.location.hash || '#/' });
  }, []);

  return (
    <div className="relative flex h-full w-full bg-agent-muted/30">
      {sidebarCollapsed ? (
        // 收起态：窄 rail 只放展开按钮。新建对话仍可用 Cmd+N / 菜单触发。
        <div className="flex h-full w-10 flex-shrink-0 flex-col items-center border-r border-agent-border/60 bg-agent-muted/70 py-2 backdrop-blur-md">
          <button
            type="button"
            onClick={toggleSidebarCollapsed}
            className="flex h-7 w-7 items-center justify-center rounded-full text-agent-muted-foreground transition-colors duration-200 hover:bg-agent-foreground/5 hover:text-agent-foreground"
            title="展开侧边栏"
            aria-label="展开侧边栏"
          >
            <LuPanelLeftOpen className="h-3.5 w-3.5" />
          </button>
        </div>
      ) : (
        <>
          <div className="h-full flex-shrink-0" style={{ width: sidebarWidth }}>
            <AgentSidebar
              data={data}
              rightPanel={rightPanel}
              onToggleRightPanel={toggleRightPanel}
              chatSlots={packChatSlots}
              onCollapse={toggleSidebarCollapsed}
            />
          </div>
          {/* Drag handle — 1px wide, 4px hit area via padding. */}
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="调整侧边栏宽度"
            onMouseDown={() => setIsResizing(true)}
            onDoubleClick={resetWidth}
            className="w-1 flex-shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-agent-border/60"
            title="拖拽调整侧边栏宽度（双击恢复默认）"
          />
        </>
      )}
      <div className="h-full min-w-0 flex-1 p-2">
        <div className="flex h-full w-full">
          <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden rounded-agent-lg bg-agent-canvas shadow-sm">
            <div className="min-h-0 flex-1 overflow-hidden">
              <Outlet
                context={{
                  ...data,
                  registerChatMessageSender,
                  sendChatMessage,
                  inspectTask,
                } satisfies AgentOutletContext}
              />
            </div>
            <InsightsConsentBanner />
          </div>
          {(rightPanel !== null || inspectedTask !== null) && (
            <>
              <div
                role="separator"
                aria-orientation="vertical"
                aria-label="调整右侧面板宽度"
                onMouseDown={() => setIsTerminalResizing(true)}
                onDoubleClick={resetTerminalWidth}
                className="w-1 flex-shrink-0 cursor-col-resize bg-transparent transition-colors hover:bg-agent-border/60"
                title="拖拽调整右侧面板宽度（双击恢复默认）"
              />
              <div
                className={`h-full flex-shrink-0 overflow-hidden rounded-agent-lg shadow-sm ${
                  rightPanel === 'terminal' && !inspectedTask ? 'bg-[#0b0b0c]' : 'bg-agent-canvas'
                }`}
                style={{ width: terminalWidth }}
                data-testid={inspectedTask ? 'task-process-dock' : 'terminal-panel'}
              >
                {inspectedTask ? (
                  <TaskProcessPanel
                    inspected={inspectedTask}
                    onClose={closeTaskProcess}
                    onShowTerminal={showTerminalFromTask}
                  />
                ) : rightPanel === 'terminal' ? (
                  <TerminalPanel
                    onClose={closeRightPanel}
                    onShowTaskProcess={recentTask ? showRecentTask : undefined}
                    taskProcessTitle={recentTask?.title}
                  />
                ) : (
                  (() => {
                    const slot = packChatSlots.find((s) => s.slotId === rightPanel);
                    if (!slot) return null;
                    const SlotPanel = slot.Component;
                    return (
                      <SlotPanel
                        chatId={chatId ?? ''}
                        onClose={closeRightPanel}
                        onSubmitToChat={sendChatMessage}
                      />
                    );
                  })()
                )}
              </div>
            </>
          )}
        </div>
      </div>
      {/* W4-1 审批弹窗挂在 layout 层而非 chat 视图：审批请求属于正在跑的
          回合，回合不因用户切走页面而暂停——挂在 AgentChatView 下时，用户
          导航到首页/别的对话会让模态永不渲染，请求只能等超时 fail-closed。 */}
      {isElectron() && <ApprovalModalHost />}
      {/* W8 提问卡片同理：挂在 layout 层，页面切换不丢待答问题。 */}
      {isElectron() && <AskUserModalHost />}
    </div>
  );
}
