/**
 * AgentSidebar — parity rewrite that mirrors the visual + interaction
 * surface of `deeppath/apps/web/src/app/agent/AgentSidebar.tsx`.
 *
 * ## Agent picker semantics (this was a UX bug — read before changing!)
 *
 * Two distinct concepts share the agent list, and conflating them was the
 * original sin of the first cut:
 *
 *   • **selectedAgentId** — "which agent will the NEXT `+ 新对话` use".
 *     Driven by user clicks in the sidebar. Persists until the user clicks
 *     another agent. Defaults to this flavor's builtin expert on first load
 *     （场景产品 → 包的主打专家；无包产品 → shell 默认智能体）.
 *   • **activeChatAgentId** — "which agent the CURRENT chat is bound to"
 *     (URL-driven, can't be re-bound without a backend endpoint). Purely
 *     informational in the sidebar — there's no UI for switching mid-chat
 *     yet.
 *
 * The original code computed `displayAgentId = activeChatAgentId ?? selectedAgentId`
 * and lit up THAT row. Result: when the user was inside a chat and clicked a
 * DIFFERENT agent, the click silently mutated `selectedAgentId`, but the
 * highlight stayed pinned to the chat's existing agent → the user saw zero
 * feedback and assumed the button was broken. They only noticed selection
 * worked after clicking `+ 新对话` and seeing the new agent in the new chat.
 *
 * Current behavior:
 *   1. **Row highlight always tracks `selectedAgentId`** — click = visible
 *      response, no exceptions.
 *   2. **Current chat's agent gets a "当前" pill** — small badge to the
 *      right of the row, never blocks selection feedback.
 *   3. **Navigating to a chat auto-syncs `selectedAgentId = chat.agentId`**
 *      via a useEffect on `activeChatAgentId`. This way the user's mental
 *      model "I'm working with agent X" stays consistent: switching chat
 *      switches the selection; clicking an agent overrides it.
 *   4. **When `selectedAgentId !== activeChatAgentId`** (the user has
 *      explicitly diverged), an inline `+ 用「name」新建对话` CTA appears
 *      below the agent list. This makes the "your click set up a new chat"
 *      contract impossible to miss.
 *
 * ## Other intentional differences from the cloud sibling
 *
 *   • Active chat id comes from the URL (`useParams().chatId`) rather than
 *     a Context (we drive navigation, not vice versa).
 *   • Agent CRUD lives on `/settings?section=agents`（侧栏「智能体管理」），
 *     not a modal. Custom agents show up in ChatInput's expert picker.
 *   • Cmd+N / Cmd+T trigger `menu:new-chat` / `menu:open-terminal` via the
 *     preload bridge; we subscribe here so the shortcuts work regardless of
 *     focused window. The terminal is a toggleable panel BESIDE the chat
 *     (owned by AgentLayout), not a route and not a separate window.
 *
 * Layout map:
 *
 *   ┌─────────────────────────────────┐
 *   │ ✨ Product Agent          +•   │ ← + has a color dot of the selected agent
 *   ├─────────────────────────────────┤
 *   │  v 专家团队 · 3                 │
 *   │   ● 教练         (selected)     │ ← highlight = selectedAgentId
 *   │   ● 文学顾问            · 当前  │ ← "当前" pill = activeChatAgentId
 *   │   ● 哲学家                      │
 *   │   + 用「教练」新建对话          │ ← inline CTA when sel ≠ current
 *   ├──── divider ────────────────────┤
 *   │ ＋ 新对话                       │  ← 只打开落地页，有内容才落库
 *   │ ⬡ 智能体管理                     │ ← /settings?section=agents（独立页）
 *   │ ⬡ Skill 设置                    │ ← /settings?section=skills（独立页）
 *   │ 🔌 MCP 设置                     │ ← /settings?section=mcp（独立页）
 *   │  v 会话 · 12                📁+ │ ← 📁+ 新建项目
 *   │  v 📁 项目A · 3      (hover: +✏📂🗑)│ ← 项目组：折叠/新建/重命名/换文件夹/删
 *   │   ...（项目内对话）              │
 *   │   今天                          │
 *   │   ...（无项目对话，按日期分组）  │ ← 无项目排在项目分组之后
 *   ├─────────────────────────────────┤
 *   │ ▢_ 终端                 ⌘T     │
 *   │ ⚙ 设置                          │ ← /settings（模型 + 洞察/遥测/用量/搜索/安全）
 *   └─────────────────────────────────┘
 *
 * 项目模式：项目 = 名字 + 绑定文件夹（ProjectRegistry，electron-store）。
 * 项目内对话的 agent 文件读写与命令执行被硬沙箱在该文件夹内（见
 * tool-router.ts ToolExecContext.projectRoot）；无项目对话不沙箱。
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  LuChevronDown,
  LuChevronUp,
  LuMessageSquare,
  LuCloudCog,
  LuTrash2,
  LuLoaderCircle,
  LuTerminal,
  LuSettings,
  LuPlus,
  LuPanelLeftClose,
  LuBlocks,
  LuBot,
  LuFolder,
  LuFolderOpen,
  LuFolderPlus,
  LuPencil,
  LuPlug,
} from 'react-icons/lu';
import { parseChatTitle } from '@/lib/chat-title';
import { getDateGroupLabel, getDateGroupPriority } from '@/lib/date-groups';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';
import {
  createProject,
  deleteProject,
  listProjects,
  updateProject,
  type LocalChat,
  type LocalChatAgent,
  type LocalProject,
} from '@/lib/local-api';
import type { UseChatsAndAgentsResult } from '@/hooks/useChatsAndAgents';
import type { PackChatSlotContribution } from '@/packs/registry';
import type { RightPanelState } from '@/layouts/AgentLayout';
// 必须 import 而不是写 src="/favicon.png"：public/ 下的资源 Vite 永远按绝对路径
// /favicon.png 输出，dev 模式下 dev server 提供根路径所以能加载，但打包成
// Electron 后渲染进程走 file:// 协议，/favicon.png 会被解析成文件系统根目录
// 下的 favicon.png（必然 404）。改成模块导入后，Vite 会把图片放到 dist/assets
// 下并发出 base-relative URL，配合 vite.config.ts 的 `base: './'` 在两种模式
// 下都能正确加载。
import { getBrandLogoUrl, BRAND_NAME } from '@/brand';

const DEFAULT_DOT_COLOR = '#7c3aed';

function agentInitial(agent: LocalChatAgent | null): string {
  const name = (agent?.name || '').trim();
  return name ? name[0].toUpperCase() : 'A';
}

function AgentDot({
  agent,
  size = 18,
}: {
  agent: LocalChatAgent | null;
  size?: number;
}) {
  const color = agent?.color || DEFAULT_DOT_COLOR;
  return (
    <span
      className="inline-flex flex-shrink-0 items-center justify-center rounded-full text-[10px] font-semibold text-white shadow-sm"
      style={{ width: size, height: size, backgroundColor: color }}
    >
      {agentInitial(agent)}
    </span>
  );
}

interface AgentSidebarProps {
  data: UseChatsAndAgentsResult;
  /** 右侧栏当前打开的面板：null / 'terminal' / 包槽位 id（互斥）。 */
  rightPanel: RightPanelState;
  /** 切换右侧栏面板显隐——面板不是路由也不是独立窗口，只是布局里的一栏。 */
  onToggleRightPanel: (kind: string) => void;
  /** 包注册的聊天页槽位（分段控件里每个槽位一个按钮）；空 = 只有终端。 */
  chatSlots: readonly PackChatSlotContribution[];
  /** 收起侧边栏（AgentLayout 换成窄 rail，展开按钮在 rail 上）。 */
  onCollapse: () => void;
}

export function AgentSidebar({
  data,
  rightPanel,
  onToggleRightPanel,
  chatSlots,
  onCollapse,
}: AgentSidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { chatId: currentChatId } = useParams<{ chatId?: string }>();
  const bridge = getElectronBridge();
  const onSettingsPage = location.pathname === '/settings';
  // /settings?section=skills|mcp|agents|（缺省 = 综合设置）—— 各自高亮。
  const settingsSection = useMemo(
    () => new URLSearchParams(location.search).get('section'),
    [location.search],
  );
  const onGeneralSettings =
    onSettingsPage &&
    settingsSection !== 'skills' &&
    settingsSection !== 'mcp' &&
    settingsSection !== 'agents';
  const onNewChatHome = !currentChatId && !onSettingsPage;

  const {
    chats,
    agents,
    isLoading: isChatLoading,
    error,
    setSelectedAgentId,
    deleteChat,
    isLoadingMoreChats,
    hasMoreChats,
    loadMoreChats,
  } = data;

  const [isMac, setIsMac] = useState(false);

  useEffect(() => {
    if (typeof navigator !== 'undefined') {
      setIsMac(/Mac|iPod|iPhone|iPad/.test(navigator.platform));
    }
  }, []);
  const [confirmDeleteChatId, setConfirmDeleteChatId] = useState<string | null>(
    null,
  );
  const [deletingChatId, setDeletingChatId] = useState<string | null>(null);
  const [chatsExpanded, setChatsExpanded] = useState(true);
  const chatScrollRef = useRef<HTMLDivElement>(null);

  // ───── 项目模式 ─────
  // 项目列表从 local-backend 拉取（electron-store 持久化）。会话按
  // projectId 分组：项目分组在上（每个项目一个可折叠分组，组头支持内联
  // 管理：新建对话 / 重命名 / 换文件夹 / 删除），无项目对话在下（日期分组）。
  const [projects, setProjects] = useState<LocalProject[]>([]);
  const [collapsedProjectIds, setCollapsedProjectIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [renamingProjectId, setRenamingProjectId] = useState<string | null>(null);
  const [renamingValue, setRenamingValue] = useState('');
  const [confirmDeleteProjectId, setConfirmDeleteProjectId] = useState<string | null>(null);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);

  const fetchProjects = useCallback(async () => {
    if (!isElectron()) return;
    try {
      const res = await listProjects();
      setProjects(res.projects || []);
    } catch (err) {
      console.error('获取项目列表失败:', err);
      setProjectError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void fetchProjects();
  }, [fetchProjects]);

  const handleCreateProject = useCallback(async () => {
    if (!isElectron()) return;
    setProjectError(null);
    try {
      const result = await bridge?.local?.selectDirectory({
        title: '选择项目文件夹',
      });
      if (!result || result.canceled || result.filePaths.length === 0) return;
      const folderPath = result.filePaths[0];
      // 默认用文件夹名做项目名，用户可随后内联重命名。
      const baseName =
        folderPath.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '新项目';
      await createProject({ name: baseName, folderPath });
      await fetchProjects();
    } catch (err) {
      setProjectError(err instanceof Error ? err.message : String(err));
    }
  }, [bridge, fetchProjects]);

  const handleRenameProject = useCallback(
    async (projectId: string) => {
      const name = renamingValue.trim();
      setRenamingProjectId(null);
      if (!name) return;
      try {
        await updateProject(projectId, { name });
        await fetchProjects();
      } catch (err) {
        setProjectError(err instanceof Error ? err.message : String(err));
      }
    },
    [renamingValue, fetchProjects],
  );

  const handleChangeProjectFolder = useCallback(
    async (projectId: string) => {
      if (!isElectron()) return;
      setProjectError(null);
      try {
        const result = await bridge?.local?.selectDirectory({
          title: '重新选择项目文件夹',
        });
        if (!result || result.canceled || result.filePaths.length === 0) return;
        await updateProject(projectId, { folderPath: result.filePaths[0] });
        await fetchProjects();
      } catch (err) {
        setProjectError(err instanceof Error ? err.message : String(err));
      }
    },
    [bridge, fetchProjects],
  );

  const handleDeleteProject = useCallback(
    async (projectId: string) => {
      if (deletingProjectId === projectId) return;
      // 与会话删除同款两段确认：第一次点击武装红色按钮，第二次才真删。
      if (confirmDeleteProjectId !== projectId) {
        setConfirmDeleteProjectId(projectId);
        return;
      }
      try {
        setDeletingProjectId(projectId);
        // 后端会把该项目下的会话降级为无项目对话（不删会话）。
        await deleteProject(projectId);
        setConfirmDeleteProjectId(null);
        await fetchProjects();
        await data.refreshChats();
      } catch (err) {
        setProjectError(err instanceof Error ? err.message : String(err));
      } finally {
        setDeletingProjectId(null);
      }
    },
    [confirmDeleteProjectId, deletingProjectId, fetchProjects, data],
  );

  const toggleProjectCollapsed = useCallback((projectId: string) => {
    setCollapsedProjectIds((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }, []);

  // Keep the shared "next new chat" agent aligned with the current chat. The
  // actual picker now lives in ChatInput, so the sidebar no longer renders the
  // expert list itself.
  const activeChatAgentId = useMemo(() => {
    if (!currentChatId) return null;
    return chats.find((c) => c.id === currentChatId)?.agentId ?? null;
  }, [chats, currentChatId]);

  // Auto-sync `selectedAgentId` when the user navigates to a chat. Without
  // this, the user lands in chat A (built on agent X), opens the sidebar,
  // sees agent Y still highlighted from a stale selection, and gets confused
  // about which agent is "active". Following the URL keeps the mental model
  // straight: "the agent I'm currently working with is the highlighted one".
  // Any explicit click in the sidebar overrides this back to the clicked
  // agent (see handlePickAgent).
  useEffect(() => {
    if (activeChatAgentId) setSelectedAgentId(activeChatAgentId);
  }, [activeChatAgentId, setSelectedAgentId]);

  // 「新对话」只打开落地页，不落库。有内容才在 EmptyChatGate 提交时
  // createChat。项目组头的「+」带上 projectId，落地页预选该项目。
  const handleOpenNewChat = useCallback(
    (projectId?: string) => {
      if (projectId) {
        navigate(`/agent?projectId=${encodeURIComponent(projectId)}`);
        return;
      }
      navigate('/agent');
    },
    [navigate],
  );

  const normalizedChats = useMemo(() => {
    return chats
      .map((chat) => {
        const updated = chat.updatedAt ? new Date(chat.updatedAt) : null;
        const created = new Date(chat.createdAt);
        const sortDate =
          updated && !Number.isNaN(updated.getTime())
            ? updated
            : !Number.isNaN(created.getTime())
              ? created
              : new Date();
        const { displayTitle, isAutomation } = parseChatTitle(chat.title);
        return {
          id: chat.id,
          title: displayTitle || '新会话',
          isAutomation,
          isPinned: chat.isPinned,
          sortDate,
          agentId: chat.agentId ?? null,
          projectId: chat.projectId ?? null,
        };
      })
      .sort((a, b) => {
        if (a.isPinned !== b.isPinned) return a.isPinned ? -1 : 1;
        return b.sortDate.getTime() - a.sortDate.getTime();
      });
  }, [chats]);

  // 顶层按项目分组：无项目对话（含项目已被删但列表还没刷新的孤儿会话）
  // 保持原有的日期分组；每个项目一个分组，组内按 pin + 时间排序。
  const knownProjectIds = useMemo(
    () => new Set(projects.map((p) => p.id)),
    [projects],
  );

  const noProjectChats = useMemo(
    () =>
      normalizedChats.filter(
        (c) => !c.projectId || !knownProjectIds.has(c.projectId),
      ),
    [normalizedChats, knownProjectIds],
  );

  const projectGroups = useMemo(
    () =>
      projects.map((project) => ({
        project,
        items: normalizedChats.filter((c) => c.projectId === project.id),
      })),
    [projects, normalizedChats],
  );

  const chatGroups = useMemo(() => {
    const map = new Map<
      string,
      {
        label: string;
        priority: number;
        items: typeof normalizedChats;
      }
    >();
    // 置顶会话独立成组排在所有日期分组之前：pin-first 排序若只带进日期
    // 分组，「5 天前置顶的会话」会排在「今天」的普通会话之后，置顶语义
    // 就只剩组内有效——与用户点图钉时的预期不符。
    noProjectChats.forEach((chat) => {
      const label = chat.isPinned ? '置顶' : getDateGroupLabel(chat.sortDate);
      if (!map.has(label)) {
        map.set(label, {
          label,
          priority: chat.isPinned ? 0 : getDateGroupPriority(label),
          items: [],
        });
      }
      map.get(label)!.items.push(chat);
    });
    return Array.from(map.values()).sort((a, b) => a.priority - b.priority);
  }, [noProjectChats]);

  const handleDeleteChat = useCallback(
    async (id: string) => {
      if (deletingChatId === id) return;
      // Two-step confirm — first click arms the red button, second click
      // actually deletes. Matches the cloud sibling exactly so users don't
      // get muscle-memory whiplash.
      if (confirmDeleteChatId !== id) {
        setConfirmDeleteChatId(id);
        return;
      }
      try {
        setDeletingChatId(id);
        const ok = await deleteChat(id);
        if (ok) {
          setConfirmDeleteChatId(null);
          if (id === currentChatId) navigate('/agent');
        }
      } finally {
        setDeletingChatId(null);
      }
    },
    [confirmDeleteChatId, deleteChat, deletingChatId, currentChatId, navigate],
  );

  // 单条会话行——无项目日期分组和项目分组共用同一个渲染，避免两份 JSX。
  const renderChatRow = (chat: (typeof normalizedChats)[number]) => {
    const isCurrent = currentChatId === chat.id;
    const isConfirmingDelete = confirmDeleteChatId === chat.id;
    const isDeleting = deletingChatId === chat.id;
    const chatAgent = agents.find((a) => a.id === chat.agentId) ?? null;
    return (
      <div
        key={chat.id}
        className="group/item relative"
        data-testid="sidebar-chat-row"
        data-chat-id={chat.id}
      >
        <button
          type="button"
          onClick={() => {
            setConfirmDeleteChatId(null);
            navigate(`/agent/${chat.id}`);
          }}
          className={[
            'flex h-8 w-full min-w-0 items-center gap-1.5 rounded-full px-3 text-sm transition-colors duration-200',
            isCurrent
              ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
              : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
          ].join(' ')}
          title={
            chat.isAutomation
              ? `[由自动化触发] ${chat.title}`
              : chat.title
          }
        >
          {chat.isPinned && (
            <span
              className="shrink-0 text-[10px]"
              aria-label="已置顶"
            >
              📌
            </span>
          )}
          {chat.isAutomation && (
            <LuCloudCog
              className="h-3 w-3 shrink-0 text-agent-muted-foreground"
              aria-label="由自动化触发"
            />
          )}
          <AgentDot agent={chatAgent} size={16} />
          <span className="min-w-0 truncate leading-none">
            {chat.title}
          </span>
        </button>
        {/* Gradient mask so the trash button doesn't paint
            over the chat title — fade matches the row's
            own background (canvas when selected, muted when
            hovered). */}
        <div
          aria-hidden="true"
          className={[
            'pointer-events-none absolute inset-y-0 right-0 w-12 rounded-r-full transition-opacity duration-200',
            isConfirmingDelete
              ? isCurrent
                ? 'bg-gradient-to-l from-agent-canvas via-agent-canvas/95 to-transparent opacity-100'
                : 'bg-gradient-to-l from-agent-muted via-agent-muted/95 to-transparent opacity-100'
              : isCurrent
                ? 'bg-gradient-to-l from-agent-canvas via-agent-canvas/95 to-transparent opacity-0 group-hover/item:opacity-100'
                : 'bg-gradient-to-l from-agent-muted via-agent-muted/95 to-transparent opacity-0 group-hover/item:opacity-100',
          ].join(' ')}
        />
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            void handleDeleteChat(chat.id);
          }}
          disabled={isDeleting}
          className={[
            'absolute right-1 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full transition-all duration-200',
            isConfirmingDelete
              ? 'bg-agent-destructive/10 text-agent-destructive opacity-100 hover:bg-agent-destructive/20'
              : 'text-agent-muted-foreground opacity-0 hover:bg-agent-foreground/5 hover:text-agent-destructive group-hover/item:opacity-100 focus:opacity-100',
            'disabled:cursor-not-allowed disabled:opacity-100',
          ].join(' ')}
          title={
            isConfirmingDelete
              ? '再次点击确认删除'
              : '删除会话'
          }
          aria-label={
            isConfirmingDelete
              ? '再次点击确认删除'
              : '删除会话'
          }
          data-testid="sidebar-chat-delete"
        >
          <LuTrash2
            className={[
              'h-3.5 w-3.5',
              isDeleting ? 'animate-pulse' : '',
            ].join(' ')}
          />
        </button>
      </div>
    );
  };

  // Cmd+N from the app menu — wired in src/main.ts:createMenu (sends
  // `menu:new-chat`). The bridge methods are optional because non-Electron
  // dev previews don't have them.
  useEffect(() => {
    if (!bridge?.onMenuNewChat) return;
    bridge.onMenuNewChat(() => {
      handleOpenNewChat();
    });
    return () => {
      bridge.offMenuNewChat?.();
    };
  }, [bridge, handleOpenNewChat]);

  // Auto-load next page when the chat scroller approaches the bottom.
  // Throttled by `isLoadingMoreChats` inside the hook.
  useEffect(() => {
    const container = chatScrollRef.current;
    if (!container || !chatsExpanded) return;

    const maybeLoadMore = () => {
      if (!hasMoreChats || isLoadingMoreChats) return;
      const nearBottom =
        container.scrollTop + container.clientHeight >=
        container.scrollHeight - 60;
      if (nearBottom) void loadMoreChats();
    };
    container.addEventListener('scroll', maybeLoadMore, { passive: true });
    const tickId = window.requestAnimationFrame(maybeLoadMore);
    return () => {
      container.removeEventListener('scroll', maybeLoadMore);
      window.cancelAnimationFrame(tickId);
    };
  }, [
    hasMoreChats,
    isLoadingMoreChats,
    loadMoreChats,
    chatsExpanded,
    normalizedChats.length,
  ]);

  // Cmd+T from the app menu — wired in src/main.ts:createMenu (sends
  // `menu:open-terminal`). Same pattern as `menu:new-chat` above. The
  // terminal is a toggleable panel beside the chat, so this just flips
  // the layout state owned by AgentLayout.
  useEffect(() => {
    if (!bridge?.onMenuOpenTerminal) return;
    bridge.onMenuOpenTerminal(() => {
      onToggleRightPanel('terminal');
    });
    return () => {
      bridge.offMenuOpenTerminal?.();
    };
  }, [bridge, onToggleRightPanel]);

  const hasElectron = isElectron();

  return (
    <div className="flex h-full w-full flex-col border-r border-agent-border/60 bg-agent-muted/70 backdrop-blur-md">
      {/* ───── Brand + actions ───── */}
      <div className="flex h-12 flex-shrink-0 items-center justify-between px-3">
        <div className="flex min-w-0 items-center gap-2">
          {/*
            品牌 logo —— src 用 module-imported asset，不要写 /favicon.png（见
            文件顶部 import 的注释）。shell 默认是中性通用图标；产品品牌
            logo 由激活包的 web 模块在注册时注入（getBrandLogoUrl）。
          */}
          <img
            src={getBrandLogoUrl()}
            alt={BRAND_NAME}
            className="h-6 w-6 flex-shrink-0 select-none"
            draggable={false}
          />
          <span className="truncate text-sm font-semibold tracking-tight text-agent-foreground">
            {BRAND_NAME}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onCollapse}
            className="flex h-7 w-7 items-center justify-center rounded-full text-agent-muted-foreground transition-colors duration-200 hover:bg-agent-foreground/5 hover:text-agent-foreground"
            title="收起侧边栏"
            aria-label="收起侧边栏"
          >
            <LuPanelLeftClose className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* ───── 会话 ───── */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {/* 新对话 + 智能体 / Skill / MCP 设置入口 — 横版行按钮，各自打开独立设置页 */}
        <div className="flex-shrink-0 space-y-0.5 px-3 pb-0.5">
          <button
            type="button"
            onClick={() => handleOpenNewChat()}
            className={[
              'flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
              onNewChatHome
                ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
                : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
            ].join(' ')}
            title="新建对话"
            data-testid="sidebar-new-chat"
          >
            <LuPlus className="h-4 w-4" />
            <span>新对话</span>
          </button>
          <button
            type="button"
            onClick={() => navigate('/settings?section=agents')}
            className={[
              'flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
              onSettingsPage && settingsSection === 'agents'
                ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
                : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
            ].join(' ')}
            title="智能体管理"
            data-testid="sidebar-agent-settings"
          >
            <LuBot className="h-4 w-4" />
            <span>智能体管理</span>
          </button>
          <button
            type="button"
            onClick={() => navigate('/settings?section=skills')}
            className={[
              'flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
              onSettingsPage && settingsSection === 'skills'
                ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
                : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
            ].join(' ')}
            title="Skill 设置"
            data-testid="sidebar-skill-settings"
          >
            <LuBlocks className="h-4 w-4" />
            <span>Skill 设置</span>
          </button>
          <button
            type="button"
            onClick={() => navigate('/settings?section=mcp')}
            className={[
              'flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
              onSettingsPage && settingsSection === 'mcp'
                ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
                : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
            ].join(' ')}
            title="MCP 设置"
            data-testid="sidebar-mcp-settings"
          >
            <LuPlug className="h-4 w-4" />
            <span>MCP 设置</span>
          </button>
        </div>
        <div className="flex flex-shrink-0 items-center justify-between py-1.5 pl-4 pr-3">
          <button
            type="button"
            onClick={() => setChatsExpanded((v) => !v)}
            className="flex items-center text-xs font-semibold tracking-wider text-agent-muted-foreground transition-colors hover:text-agent-foreground"
          >
            <span className="mr-1">
              {chatsExpanded ? (
                <LuChevronUp className="h-3 w-3" />
              ) : (
                <LuChevronDown className="h-3 w-3" />
              )}
            </span>
            会话
            {normalizedChats.length > 0 && (
              <span className="ml-1.5 text-[10px] font-normal text-agent-muted-foreground/70">
                · {normalizedChats.length}
              </span>
            )}
          </button>
          <div className="flex items-center gap-1">
            {/* 新建项目：选文件夹 → 以文件夹名建项目，之后可在组头重命名 */}
            {hasElectron && (
              <button
                type="button"
                onClick={() => void handleCreateProject()}
                className="flex h-6 w-6 items-center justify-center rounded-full text-agent-muted-foreground transition-colors duration-200 hover:bg-agent-foreground/5 hover:text-agent-foreground"
                title="新建项目（绑定文件夹，项目内对话的文件操作被限制在该文件夹）"
                aria-label="新建项目"
              >
                <LuFolderPlus className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </div>

        {chatsExpanded && (
          <div
            ref={chatScrollRef}
            className="flex-1 overflow-y-auto px-2 pb-1"
          >
            {isChatLoading && chats.length === 0 && projects.length === 0 ? (
              <div className="flex items-center justify-center py-4 text-xs text-agent-muted-foreground">
                <LuLoaderCircle className="mr-1.5 h-3 w-3 animate-spin" />
                加载中...
              </div>
            ) : chatGroups.length === 0 && projectGroups.length === 0 ? (
              <div className="flex flex-col items-center gap-1.5 py-6 text-xs text-agent-muted-foreground">
                <LuMessageSquare className="h-4 w-4 text-agent-muted-foreground/60" />
                暂无会话
              </div>
            ) : (
              <>
                {/* 项目分组在前：组头可折叠，hover 出内联管理动作 */}
                {projectGroups.map(({ project, items }) => (
                  <div key={project.id} className="mb-1">
                    <div className="group/proj relative">
                      {renamingProjectId === project.id ? (
                        <form
                          className="flex items-center px-3 pb-1 pt-2"
                          onSubmit={(event) => {
                            event.preventDefault();
                            void handleRenameProject(project.id);
                          }}
                        >
                          <input
                            autoFocus
                            value={renamingValue}
                            onChange={(event) => setRenamingValue(event.target.value)}
                            onBlur={() => void handleRenameProject(project.id)}
                            onKeyDown={(event) => {
                              if (event.key === 'Escape') setRenamingProjectId(null);
                            }}
                            className="h-6 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                          />
                        </form>
                      ) : (
                        <>
                          <button
                            type="button"
                            onClick={() => toggleProjectCollapsed(project.id)}
                            className="flex w-full min-w-0 items-center px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wider text-agent-muted-foreground/80 transition-colors hover:text-agent-foreground"
                            title={`${project.name}\n${project.folderPath}`}
                          >
                            <span className="mr-1">
                              {collapsedProjectIds.has(project.id) ? (
                                <LuChevronDown className="h-3 w-3" />
                              ) : (
                                <LuChevronUp className="h-3 w-3" />
                              )}
                            </span>
                            <LuFolder className="mr-1 h-3 w-3 shrink-0" />
                            <span className="min-w-0 truncate normal-case">
                              {project.name}
                            </span>
                            {items.length > 0 && (
                              <span className="ml-1 shrink-0 font-normal">
                                · {items.length}
                              </span>
                            )}
                          </button>
                          {/* 内联管理：新建对话 / 重命名 / 换文件夹 / 删除（两段确认） */}
                          <div className="absolute right-1 top-1/2 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity group-hover/proj:opacity-100">
                            <button
                              type="button"
                              onClick={() => handleOpenNewChat(project.id)}
                              className="flex h-5 w-5 items-center justify-center rounded-full text-agent-muted-foreground hover:bg-agent-foreground/10 hover:text-agent-foreground"
                              title="在此项目下新建对话"
                            >
                              <LuPlus className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setRenamingProjectId(project.id);
                                setRenamingValue(project.name);
                              }}
                              className="flex h-5 w-5 items-center justify-center rounded-full text-agent-muted-foreground hover:bg-agent-foreground/10 hover:text-agent-foreground"
                              title="重命名项目"
                            >
                              <LuPencil className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleChangeProjectFolder(project.id)}
                              className="flex h-5 w-5 items-center justify-center rounded-full text-agent-muted-foreground hover:bg-agent-foreground/10 hover:text-agent-foreground"
                              title="更换绑定文件夹"
                            >
                              <LuFolderOpen className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => void handleDeleteProject(project.id)}
                              disabled={deletingProjectId === project.id}
                              className={`flex h-5 w-5 items-center justify-center rounded-full transition-colors ${
                                confirmDeleteProjectId === project.id
                                  ? 'bg-agent-destructive/10 text-agent-destructive hover:bg-agent-destructive/20'
                                  : 'text-agent-muted-foreground hover:bg-agent-foreground/10 hover:text-agent-destructive'
                              }`}
                              title={
                                confirmDeleteProjectId === project.id
                                  ? '再次点击确认删除（会话会保留为无项目对话）'
                                  : '删除项目（会话保留为无项目对话）'
                              }
                            >
                              <LuTrash2
                                className={`h-3 w-3 ${deletingProjectId === project.id ? 'animate-pulse' : ''}`}
                              />
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                    {!collapsedProjectIds.has(project.id) && (
                      <div className="space-y-0.5">
                        {items.length === 0 ? (
                          <div className="px-3 py-1 text-[11px] text-agent-muted-foreground/60">
                            暂无会话 — hover 项目名点 + 新建
                          </div>
                        ) : (
                          items.map((chat) => renderChatRow(chat))
                        )}
                      </div>
                    )}
                  </div>
                ))}
                {/* 无项目对话排在项目分组之后：保持原有日期分组 */}
                {chatGroups.map((group) => (
                  <div key={group.label} className="mb-1">
                    <div className="px-3 pb-1 pt-2 text-[10px] font-medium uppercase tracking-wider text-agent-muted-foreground/80">
                      {group.label}
                    </div>
                    <div className="space-y-0.5">
                      {group.items.map((chat) => renderChatRow(chat))}
                    </div>
                  </div>
                ))}
              </>
            )}
            {(chatGroups.length > 0 || projectGroups.length > 0) && (
              <div className="px-2 py-2 text-center text-[11px] text-agent-muted-foreground">
                {isLoadingMoreChats ? (
                  <span className="inline-flex items-center gap-1">
                    <LuLoaderCircle className="h-3 w-3 animate-spin" />
                    加载更多...
                  </span>
                ) : hasMoreChats ? (
                  '继续下滑加载更多'
                ) : (
                  `共 ${normalizedChats.length} 个会话`
                )}
              </div>
            )}
          </div>
        )}
      </div>

      {(error || projectError) && (
        <div
          className="flex-shrink-0 border-t border-agent-destructive/40 bg-agent-destructive/10 px-3 py-2 text-[11px] text-agent-destructive"
          role="alert"
        >
          {error ?? projectError}
        </div>
      )}

      {/* ───── Footer: 右侧面板切换（终端 | 包槽位）+ 设置 ───── */}
      <div className="flex-shrink-0 border-t border-agent-border/40 px-3 py-2">
        {chatSlots.length === 0 ? (
          <button
            type="button"
            onClick={() => onToggleRightPanel('terminal')}
            className={[
              'flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
              rightPanel === 'terminal'
                ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
                : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-foreground',
            ].join(' ')}
            title={`${rightPanel === 'terminal' ? '关闭' : '打开'}终端面板 (${isMac ? '⌘T' : 'Ctrl+T'})`}
            data-testid="sidebar-terminal"
          >
            <LuTerminal className="h-4 w-4" />
            <span>终端</span>
            <span className="ml-auto text-[10px] text-agent-muted-foreground/70">
              {isMac ? '⌘T' : 'Ctrl+T'}
            </span>
          </button>
        ) : (
          <div
            role="group"
            aria-label="右侧面板切换"
            className="flex h-8 w-full items-center gap-0.5 rounded-full bg-agent-foreground/5 p-0.5"
          >
            <button
              type="button"
              onClick={() => onToggleRightPanel('terminal')}
              className={[
                'flex h-7 flex-1 items-center justify-center gap-1.5 rounded-full text-xs font-medium transition-colors',
                rightPanel === 'terminal'
                  ? 'bg-agent-foreground/10 text-agent-foreground'
                  : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
              ].join(' ')}
              title={`${rightPanel === 'terminal' ? '关闭' : '打开'}终端面板`}
              data-testid="sidebar-terminal"
            >
              <LuTerminal className="h-3.5 w-3.5" />
              <span>终端</span>
            </button>
            {chatSlots.map((slot) => (
              <button
                key={slot.slotId}
                type="button"
                onClick={() => onToggleRightPanel(slot.slotId)}
                className={[
                  'flex h-7 flex-1 items-center justify-center gap-1.5 rounded-full text-xs font-medium transition-colors',
                  rightPanel === slot.slotId
                    ? 'bg-agent-foreground/10 text-agent-foreground'
                    : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
                ].join(' ')}
                title={`${rightPanel === slot.slotId ? '关闭' : '打开'}${slot.title}`}
                data-testid={`sidebar-slot-${slot.slotId}`}
              >
                <slot.Icon className="h-3.5 w-3.5" />
                <span>{slot.title}</span>
              </button>
            ))}
          </div>
        )}
        <button
          type="button"
          onClick={() => navigate('/settings')}
          className={[
            'mt-0.5 flex h-8 w-full items-center gap-2 rounded-full px-3 text-sm transition-colors',
            onGeneralSettings
              ? 'bg-agent-foreground/10 font-medium text-agent-foreground'
              : 'text-agent-muted-foreground hover:bg-agent-foreground/5 hover:text-agent-foreground',
          ].join(' ')}
          title="设置"
          data-testid="sidebar-llm-settings"
        >
          <LuSettings className="h-4 w-4" />
          <span>设置</span>
        </button>
      </div>
    </div>
  );
}

// Re-export for callers that still import the type from here.
export type { LocalChat, LocalChatAgent };
