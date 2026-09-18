/**
 * AgentSidebar 交互契约：
 *   - 新对话只开落地页不落库（既有用例，见第一个 describe）；
 *   - 会话列表：置顶优先 + 时间倒序、[自动化] 标题解析、智能体首字母圆点、
 *     日期分组、空态 / 加载态 / 错误横幅、底部分页提示；
 *   - 会话行：点击导航、删除两段确认（第一次武装第二次才删）、
 *     删除当前会话回落地页、武装后点行解除武装；
 *   - 入口导航与高亮：智能体管理 / Skill / MCP / 综合设置随路由各自高亮；
 *   - 右侧面板：终端按钮快捷键提示随平台变化，包槽位渲染分段控件；
 *   - 副作用：进入会话路由同步 selectedAgentId、菜单 Cmd+N / Cmd+T 订阅与
 *     退订、滚动接近底部自动加载下一页；
 *   - 项目模式（Electron）：项目分组与折叠、孤儿会话回落日期分组、
 *     新建（文件夹名即项目名）/ 内联重命名 / 换文件夹 / 两段确认删除。
 * 智能体挑选列表已迁到 ChatInput（见组件头注释），侧栏只剩同步副作用可测。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ElectronBridge } from '@/lib/electron-bridge';
import type { UseChatsAndAgentsResult } from '@/hooks/useChatsAndAgents';
import type { LocalChat, LocalChatAgent, LocalProject } from '@/lib/local-api';
import type { RightPanelState } from '@/layouts/AgentLayout';
import type { PackChatSlotContribution } from '@/packs/registry';

// 可控桥桩：bridgeStub 为 null 时 isElectron() = false（纯浏览器预览路径），
// 项目模式用例经 enterElectron() 装上带 selectDirectory 的桥。
let bridgeStub: ElectronBridge | null = null;

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => bridgeStub !== null,
  getElectronBridge: () => bridgeStub,
}));

const listProjects = vi.fn();
const createProject = vi.fn();
const updateProject = vi.fn();
const deleteProject = vi.fn();

vi.mock('@/lib/local-api', () => ({
  listProjects: (...args: unknown[]) => listProjects(...args),
  createProject: (...args: unknown[]) => createProject(...args),
  updateProject: (...args: unknown[]) => updateProject(...args),
  deleteProject: (...args: unknown[]) => deleteProject(...args),
}));

vi.mock('@/brand', () => ({
  BRAND_NAME: '测试助手',
  getBrandLogoUrl: () => 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>',
}));

const { AgentSidebar } = await import('./AgentSidebar');

const originalPlatform = window.navigator.platform;

beforeEach(() => {
  bridgeStub = null;
  listProjects.mockReset();
  createProject.mockReset();
  updateProject.mockReset();
  deleteProject.mockReset();
  listProjects.mockResolvedValue({ projects: [] });
});

afterEach(() => {
  cleanup();
  Object.defineProperty(window.navigator, 'platform', {
    value: originalPlatform,
    configurable: true,
  });
});

const agent: LocalChatAgent = {
  id: 'local-assistant',
  slug: 'local-assistant',
  name: '电脑操作员',
  icon: null,
  color: '#4f46e5',
  description: null,
  rolePrompt: null,
  isBuiltin: true,
};

const existingChat: LocalChat = {
  id: 'chat-with-content',
  projectId: null,
  userId: 'local',
  title: '已经聊过的对话',
  agentId: agent.id,
  createdAt: '2026-01-01T00:00:00.000Z',
  updatedAt: '2026-01-01T00:01:00.000Z',
  isPinned: false,
  systemPrompt: null,
  pinnedRefs: null,
};

function makeChat(overrides: Partial<LocalChat> = {}): LocalChat {
  return {
    id: 'chat-x',
    projectId: null,
    userId: 'local',
    title: '测试会话',
    agentId: null,
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:01:00.000Z',
    isPinned: false,
    systemPrompt: null,
    pinnedRefs: null,
    ...overrides,
  };
}

function makeProject(overrides: Partial<LocalProject> = {}): LocalProject {
  return {
    id: 'proj-1',
    name: '项目甲',
    folderPath: '/tmp/proj-a',
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  };
}

/** 相对今天零点偏移 n 天的 ISO 时间（日期分组用例用）。 */
function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString();
}

/** 今天指定整点的 ISO 时间（同分组内排序用例用，避免跨组）。 */
function todayAt(hour: number): string {
  const d = new Date();
  d.setHours(hour, 0, 0, 0);
  return d.toISOString();
}

function setPlatform(platform: string) {
  Object.defineProperty(window.navigator, 'platform', { value: platform, configurable: true });
}

function baseElectronBridge(overrides: Partial<ElectronBridge> = {}): ElectronBridge {
  return {
    runtime: 'local',
    platform: 'darwin',
    local: {
      selectDirectory: vi.fn(async () => ({ canceled: true, filePaths: [] as string[] })),
      captureScreenshot: vi.fn(async () => ({ success: false as const, error: '未实现' })),
    },
    localBackend: {
      // 侧栏用例不对 request 做断言；vi.fn 保不住泛型签名，这里按接口收窄。
      request: vi.fn() as unknown as ElectronBridge['localBackend']['request'],
      startStream: vi.fn(async () => null),
      cancelStream: vi.fn(),
    },
    ...overrides,
  };
}

/** 进入 Electron 模式：listProjects 应答给定项目列表，桥带可断言的目录选择器。 */
function enterElectron(projects: LocalProject[]) {
  listProjects.mockResolvedValue({ projects });
  const selectDirectory = vi.fn(async () => ({ canceled: true, filePaths: [] as string[] }));
  bridgeStub = baseElectronBridge();
  bridgeStub.local!.selectDirectory = selectDirectory;
  return { selectDirectory };
}

function LocationProbe() {
  const loc = useLocation();
  return (
    <div data-testid="loc">
      {loc.pathname}
      {loc.search}
    </div>
  );
}

function makeData(overrides: Partial<UseChatsAndAgentsResult> = {}): UseChatsAndAgentsResult {
  return {
    chats: [existingChat],
    agents: [agent],
    isLoading: false,
    error: null,
    selectedAgentId: agent.id,
    setSelectedAgentId: vi.fn(),
    refreshChats: vi.fn(async () => {}),
    refreshAgents: vi.fn(async () => {}),
    createChat: vi.fn(),
    deleteChat: vi.fn(async () => true),
    patchChatTitle: vi.fn(),
    isLoadingMoreChats: false,
    hasMoreChats: false,
    loadMoreChats: vi.fn(async () => {}),
    ...overrides,
  };
}

interface RenderSidebarOptions {
  data?: Partial<UseChatsAndAgentsResult>;
  rightPanel?: RightPanelState;
  onToggleRightPanel?: (kind: string) => void;
  chatSlots?: readonly PackChatSlotContribution[];
  onCollapse?: () => void;
}

function renderSidebar(
  initialEntry: string,
  createChat = vi.fn(),
  options: RenderSidebarOptions = {},
) {
  const data = makeData({ createChat, ...options.data });
  const onToggleRightPanel = options.onToggleRightPanel ?? vi.fn();
  const onCollapse = options.onCollapse ?? vi.fn();

  const utils = render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route
          element={
            <>
              <AgentSidebar
                data={data}
                rightPanel={options.rightPanel ?? null}
                onToggleRightPanel={onToggleRightPanel}
                chatSlots={options.chatSlots ?? []}
                onCollapse={onCollapse}
              />
              <LocationProbe />
              <Outlet />
            </>
          }
        >
          <Route path="/" element={<div />} />
          <Route path="/agent" element={<div />} />
          <Route path="/agent/:chatId" element={<div />} />
          <Route path="/settings" element={<div />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );

  return { createChat, data, onToggleRightPanel, onCollapse, ...utils };
}

/** 按 data-chat-id 取会话行容器。 */
function chatRow(chatId: string): HTMLElement {
  const row = document.querySelector(`[data-chat-id="${chatId}"]`);
  if (!row) throw new Error(`会话行不存在: ${chatId}`);
  return row as HTMLElement;
}

describe('AgentSidebar 新对话不落库', () => {
  it('打开落地页且不调用 createChat', () => {
    const { createChat } = renderSidebar('/agent/chat-with-content');
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    expect(createChat).not.toHaveBeenCalled();
    expect(screen.getByTestId('loc').textContent).toBe('/agent');
  });

  it('连点两次也不会多出侧栏行', () => {
    renderSidebar('/agent');
    const before = screen.getAllByTestId('sidebar-chat-row').length;
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    fireEvent.click(screen.getByTestId('sidebar-new-chat'));
    expect(screen.getAllByTestId('sidebar-chat-row')).toHaveLength(before);
  });

  it('打开智能体管理页', () => {
    renderSidebar('/agent');
    fireEvent.click(screen.getByTestId('sidebar-agent-settings'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings?section=agents');
  });
});

describe('AgentSidebar 会话列表渲染', () => {
  it('渲染会话标题与绑定智能体的首字母圆点，无智能体回落为 A', () => {
    const noAgent = makeChat({ id: 'c-no-agent', title: '无智能体会话', agentId: null });
    renderSidebar('/agent', vi.fn(), { data: { chats: [existingChat, noAgent] } });

    expect(chatRow('chat-with-content').textContent).toContain('已经聊过的对话');
    expect(chatRow('chat-with-content').textContent).toContain('电');
    expect(chatRow('c-no-agent').textContent).toContain('A');
  });

  it('同一日期分组内置顶会话排在普通会话之前并带图钉标记', () => {
    const pinnedEarly = makeChat({
      id: 'c-pinned',
      title: '置顶的较早会话',
      isPinned: true,
      updatedAt: todayAt(1),
    });
    const fresh = makeChat({ id: 'c-fresh', title: '较晚的普通会话', updatedAt: todayAt(23) });
    renderSidebar('/agent', vi.fn(), { data: { chats: [fresh, pinnedEarly] } });

    const ids = screen.getAllByTestId('sidebar-chat-row').map((r) => r.getAttribute('data-chat-id'));
    expect(ids).toEqual(['c-pinned', 'c-fresh']);
    expect(screen.getByLabelText('已置顶')).toBeTruthy();
  });

  it('置顶会话跨日期分组也排在最前：独立「置顶」组优先于「今天」', () => {
    const pinnedOld = makeChat({
      id: 'c-pinned-old',
      title: '五天前置顶的会话',
      isPinned: true,
      updatedAt: daysAgo(5),
    });
    const fresh = makeChat({ id: 'c-fresh', title: '今天的普通会话', updatedAt: daysAgo(0) });
    renderSidebar('/agent', vi.fn(), { data: { chats: [fresh, pinnedOld] } });

    // 组头顺序：置顶组在「今天」之前
    expect(screen.getByText('置顶')).toBeTruthy();
    const pinnedHeader = screen.getByText('置顶');
    const todayHeader = screen.getByText('今天');
    expect(
      pinnedHeader.compareDocumentPosition(todayHeader) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 行顺序：旧的置顶会话仍在今天的普通会话之前
    const ids = screen.getAllByTestId('sidebar-chat-row').map((r) => r.getAttribute('data-chat-id'));
    expect(ids).toEqual(['c-pinned-old', 'c-fresh']);
  });

  it('[自动化] 前缀被解析为图标与提示，不进入展示标题', () => {
    const auto = makeChat({ id: 'c-auto', title: '[自动化] 每小时巡检' });
    renderSidebar('/agent', vi.fn(), { data: { chats: [auto] } });

    const rowButton = screen.getByTitle('[由自动化触发] 每小时巡检');
    expect(rowButton.textContent).toContain('每小时巡检');
    expect(rowButton.textContent).not.toContain('[自动化]');
    expect(screen.getByLabelText('由自动化触发')).toBeTruthy();
  });

  it('无项目会话按今天 / 昨天日期分组且新组在前', () => {
    const todayChat = makeChat({ id: 'c-today', title: '今天的会话', updatedAt: daysAgo(0) });
    const yesterdayChat = makeChat({ id: 'c-yesterday', title: '昨天的会话', updatedAt: daysAgo(1) });
    renderSidebar('/agent', vi.fn(), { data: { chats: [yesterdayChat, todayChat] } });

    expect(screen.getByText('今天')).toBeTruthy();
    expect(screen.getByText('昨天')).toBeTruthy();
    const ids = screen.getAllByTestId('sidebar-chat-row').map((r) => r.getAttribute('data-chat-id'));
    expect(ids).toEqual(['c-today', 'c-yesterday']);
  });

  it('空列表展示空态，加载中展示 spinner，错误走 alert 横幅', () => {
    const { unmount } = renderSidebar('/agent', vi.fn(), { data: { chats: [] } });
    expect(screen.getByText('暂无会话')).toBeTruthy();
    unmount();

    const second = renderSidebar('/agent', vi.fn(), { data: { chats: [], isLoading: true } });
    expect(screen.getByText('加载中...')).toBeTruthy();
    second.unmount();

    renderSidebar('/agent', vi.fn(), { data: { error: '列表加载失败' } });
    expect(screen.getByRole('alert').textContent).toContain('列表加载失败');
  });

  it('头部计数与底部汇总随会话数变化', () => {
    renderSidebar('/agent', vi.fn(), {
      data: { chats: [existingChat, makeChat({ id: 'c-2', title: '第二条' })] },
    });
    expect(screen.getByText('· 2')).toBeTruthy();
    expect(screen.getByText('共 2 个会话')).toBeTruthy();
  });

  it('有更多页时底部提示继续下滑，加载中提示加载更多', () => {
    const { unmount } = renderSidebar('/agent', vi.fn(), { data: { hasMoreChats: true } });
    expect(screen.getByText('继续下滑加载更多')).toBeTruthy();
    unmount();

    renderSidebar('/agent', vi.fn(), { data: { hasMoreChats: true, isLoadingMoreChats: true } });
    expect(screen.getByText('加载更多...')).toBeTruthy();
  });
});

describe('AgentSidebar 会话行交互', () => {
  it('点击会话行导航到对应会话', () => {
    renderSidebar('/agent');
    fireEvent.click(screen.getByText('已经聊过的对话'));
    expect(screen.getByTestId('loc').textContent).toBe('/agent/chat-with-content');
  });

  it('删除需两段确认：第一次武装红色按钮，第二次才调 deleteChat', async () => {
    const deleteChat = vi.fn(async () => true);
    renderSidebar('/agent', vi.fn(), { data: { deleteChat } });

    const del = screen.getByTestId('sidebar-chat-delete');
    fireEvent.click(del);
    expect(deleteChat).not.toHaveBeenCalled();
    expect(del.getAttribute('aria-label')).toBe('再次点击确认删除');

    fireEvent.click(del);
    await waitFor(() => expect(deleteChat).toHaveBeenCalledWith('chat-with-content'));
  });

  it('删除当前会话后回到落地页', async () => {
    const deleteChat = vi.fn(async () => true);
    renderSidebar('/agent/chat-with-content', vi.fn(), { data: { deleteChat } });

    const del = screen.getByTestId('sidebar-chat-delete');
    fireEvent.click(del);
    fireEvent.click(del);
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/agent'));
  });

  it('deleteChat 返回 false 时不导航且保持武装态', async () => {
    const deleteChat = vi.fn(async () => false);
    renderSidebar('/agent/chat-with-content', vi.fn(), { data: { deleteChat } });

    const del = screen.getByTestId('sidebar-chat-delete');
    fireEvent.click(del);
    fireEvent.click(del);
    await waitFor(() => expect(deleteChat).toHaveBeenCalled());
    expect(screen.getByTestId('loc').textContent).toBe('/agent/chat-with-content');
    expect(screen.getByTestId('sidebar-chat-delete').getAttribute('aria-label')).toBe(
      '再次点击确认删除',
    );
  });

  it('武装后点击会话行会解除武装并正常导航', () => {
    renderSidebar('/agent');
    const del = screen.getByTestId('sidebar-chat-delete');
    fireEvent.click(del);
    expect(del.getAttribute('aria-label')).toBe('再次点击确认删除');

    fireEvent.click(screen.getByText('已经聊过的对话'));
    expect(screen.getByTestId('loc').textContent).toBe('/agent/chat-with-content');
    expect(screen.getByTestId('sidebar-chat-delete').getAttribute('aria-label')).toBe('删除会话');
  });

  it('会话区可折叠再展开', () => {
    renderSidebar('/agent');
    fireEvent.click(screen.getByRole('button', { name: /^会话/ }));
    expect(screen.queryByTestId('sidebar-chat-row')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /^会话/ }));
    expect(screen.getByTestId('sidebar-chat-row')).toBeTruthy();
  });
});

describe('AgentSidebar 入口导航与高亮', () => {
  it('Skill / MCP / 综合设置入口分别导航到对应设置页', () => {
    renderSidebar('/agent');
    fireEvent.click(screen.getByTestId('sidebar-skill-settings'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings?section=skills');
    fireEvent.click(screen.getByTestId('sidebar-mcp-settings'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings?section=mcp');
    fireEvent.click(screen.getByTestId('sidebar-llm-settings'));
    expect(screen.getByTestId('loc').textContent).toBe('/settings');
  });

  it('当前路由对应的入口高亮：?section=skills 只亮 Skill 设置', () => {
    renderSidebar('/settings?section=skills');
    expect(screen.getByTestId('sidebar-skill-settings').className).toContain('bg-agent-foreground/10');
    expect(screen.getByTestId('sidebar-llm-settings').className).not.toContain(
      'bg-agent-foreground/10',
    );
  });

  it('/settings 无 section 时高亮底部综合设置', () => {
    renderSidebar('/settings');
    expect(screen.getByTestId('sidebar-llm-settings').className).toContain('bg-agent-foreground/10');
    expect(screen.getByTestId('sidebar-skill-settings').className).not.toContain(
      'bg-agent-foreground/10',
    );
  });

  it('当前会话行高亮，其余行不高亮', () => {
    const other = makeChat({ id: 'c-other', title: '另一条会话' });
    renderSidebar('/agent/chat-with-content', vi.fn(), {
      data: { chats: [existingChat, other] },
    });
    expect(chatRow('chat-with-content').querySelector('button')!.className).toContain(
      'bg-agent-foreground/10',
    );
    expect(chatRow('c-other').querySelector('button')!.className).not.toContain(
      'bg-agent-foreground/10',
    );
  });

  it('收起按钮回调 onCollapse', () => {
    const onCollapse = vi.fn();
    renderSidebar('/agent', vi.fn(), { onCollapse });
    fireEvent.click(screen.getByLabelText('收起侧边栏'));
    expect(onCollapse).toHaveBeenCalledTimes(1);
  });
});

describe('AgentSidebar 右侧面板切换', () => {
  it('Mac 上终端按钮提示 ⌘T，点击切换终端面板', () => {
    setPlatform('MacIntel');
    const onToggleRightPanel = vi.fn();
    renderSidebar('/agent', vi.fn(), { onToggleRightPanel });

    const btn = screen.getByTestId('sidebar-terminal');
    expect(btn.getAttribute('title')).toBe('打开终端面板 (⌘T)');
    fireEvent.click(btn);
    expect(onToggleRightPanel).toHaveBeenCalledWith('terminal');
  });

  it('非 Mac 提示 Ctrl+T；面板已打开时 title 变为关闭', () => {
    setPlatform('Linux');
    renderSidebar('/agent', vi.fn(), { rightPanel: 'terminal' });

    const btn = screen.getByTestId('sidebar-terminal');
    expect(btn.textContent).toContain('Ctrl+T');
    expect(btn.getAttribute('title')).toBe('关闭终端面板 (Ctrl+T)');
    expect(btn.className).toContain('bg-agent-foreground/10');
  });

  it('有包槽位时渲染分段控件，点击槽位切换对应面板', () => {
    const previewSlot: PackChatSlotContribution = {
      slotId: 'preview',
      title: '预览',
      Icon: ({ className }: { className?: string }) => <svg className={className} />,
      Component: () => null,
    };
    const onToggleRightPanel = vi.fn();
    renderSidebar('/agent', vi.fn(), {
      chatSlots: [previewSlot],
      rightPanel: 'preview',
      onToggleRightPanel,
    });

    const slotBtn = screen.getByTestId('sidebar-slot-preview');
    expect(slotBtn.textContent).toContain('预览');
    expect(slotBtn.className).toContain('bg-agent-foreground/10');
    fireEvent.click(slotBtn);
    expect(onToggleRightPanel).toHaveBeenCalledWith('preview');
    // 终端按钮收进分段控件，仍然可用。
    fireEvent.click(screen.getByTestId('sidebar-terminal'));
    expect(onToggleRightPanel).toHaveBeenCalledWith('terminal');
  });
});

describe('AgentSidebar 副作用', () => {
  it('进入会话路由时把 selectedAgentId 同步为该会话绑定的智能体', () => {
    const setSelectedAgentId = vi.fn();
    renderSidebar('/agent/chat-with-content', vi.fn(), { data: { setSelectedAgentId } });
    expect(setSelectedAgentId).toHaveBeenCalledWith('local-assistant');
  });

  it('落地页没有当前会话，不同步 selectedAgentId', () => {
    const setSelectedAgentId = vi.fn();
    renderSidebar('/agent', vi.fn(), { data: { setSelectedAgentId } });
    expect(setSelectedAgentId).not.toHaveBeenCalled();
  });

  it('订阅菜单新建对话事件：回调导航落地页，卸载时退订', () => {
    let menuCb: (() => void) | null = null;
    const offMenuNewChat = vi.fn();
    bridgeStub = baseElectronBridge({
      onMenuNewChat: (cb) => {
        menuCb = cb;
      },
      offMenuNewChat,
    });
    const { unmount } = renderSidebar('/agent/chat-with-content');

    expect(menuCb).not.toBeNull();
    act(() => {
      menuCb!();
    });
    expect(screen.getByTestId('loc').textContent).toBe('/agent');

    unmount();
    // 导航后 useNavigate 返回值的 identity 会变，handleOpenNewChat 随之变化
    // 导致订阅 effect 重跑（先退订再订阅），所以这里不断言精确次数——
    // 精确退订契约由下一个用例锁定。
    expect(offMenuNewChat).toHaveBeenCalled();
  });

  it('未触发导航直接卸载时，菜单订阅恰好退订一次', () => {
    const offMenuNewChat = vi.fn();
    bridgeStub = baseElectronBridge({
      onMenuNewChat: () => {},
      offMenuNewChat,
    });
    const { unmount } = renderSidebar('/agent');

    unmount();
    expect(offMenuNewChat).toHaveBeenCalledTimes(1);
  });

  it('订阅菜单打开终端事件：回调切换终端面板，卸载时退订', () => {
    let menuCb: (() => void) | null = null;
    const offMenuOpenTerminal = vi.fn();
    const onToggleRightPanel = vi.fn();
    bridgeStub = baseElectronBridge({
      onMenuOpenTerminal: (cb) => {
        menuCb = cb;
      },
      offMenuOpenTerminal,
    });
    const { unmount } = renderSidebar('/agent', vi.fn(), { onToggleRightPanel });

    act(() => {
      menuCb!();
    });
    expect(onToggleRightPanel).toHaveBeenCalledWith('terminal');

    unmount();
    expect(offMenuOpenTerminal).toHaveBeenCalledTimes(1);
  });

  it('滚动容器接近底部时自动加载下一页（测试环境零高度即触底）', async () => {
    const loadMoreChats = vi.fn(async () => {});
    renderSidebar('/agent', vi.fn(), { data: { hasMoreChats: true, loadMoreChats } });
    await waitFor(() => expect(loadMoreChats).toHaveBeenCalled());
  });

  it('没有更多会话时不触发加载', async () => {
    const loadMoreChats = vi.fn(async () => {});
    renderSidebar('/agent', vi.fn(), { data: { hasMoreChats: false, loadMoreChats } });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60));
    });
    expect(loadMoreChats).not.toHaveBeenCalled();
  });

  it('正在加载更多时不重复触发', async () => {
    const loadMoreChats = vi.fn(async () => {});
    renderSidebar('/agent', vi.fn(), {
      data: { hasMoreChats: true, isLoadingMoreChats: true, loadMoreChats },
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 60));
    });
    expect(loadMoreChats).not.toHaveBeenCalled();
  });
});

describe('AgentSidebar 项目模式（Electron）', () => {
  it('项目分组渲染在前，孤儿会话回落到无项目日期分组', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    const inProject = makeChat({ id: 'c-in', title: '项目内会话', projectId: 'proj-1' });
    const orphan = makeChat({
      id: 'c-orphan',
      title: '孤儿会话',
      projectId: 'proj-gone',
      updatedAt: daysAgo(0),
    });
    renderSidebar('/agent', vi.fn(), { data: { chats: [inProject, orphan] } });

    await screen.findByText('项目甲');
    // 项目组头计数只算真正属于该项目的会话。
    expect(screen.getByText('· 1')).toBeTruthy();
    expect(chatRow('c-in')).toBeTruthy();
    // 孤儿会话按无项目处理，进入日期分组。
    expect(chatRow('c-orphan')).toBeTruthy();
    expect(screen.getByText('今天')).toBeTruthy();
  });

  it('项目组头可折叠再展开', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    renderSidebar('/agent', vi.fn(), {
      data: { chats: [makeChat({ id: 'c-in', title: '项目内会话', projectId: 'proj-1' })] },
    });

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByText('项目甲').closest('button')!);
    expect(screen.queryByText('项目内会话')).toBeNull();

    fireEvent.click(screen.getByText('项目甲').closest('button')!);
    expect(screen.getByText('项目内会话')).toBeTruthy();
  });

  it('项目组头的 + 导航到带 projectId 的落地页', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('在此项目下新建对话'));
    expect(screen.getByTestId('loc').textContent).toBe('/agent?projectId=proj-1');
  });

  it('新建项目：选中文件夹后以文件夹名建项目并刷新列表', async () => {
    const { selectDirectory } = enterElectron([]);
    selectDirectory.mockResolvedValue({ canceled: false, filePaths: ['/tmp/演示项目'] });
    createProject.mockResolvedValue({ success: true, project: makeProject() });
    renderSidebar('/agent');

    fireEvent.click(screen.getByLabelText('新建项目'));
    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith({ name: '演示项目', folderPath: '/tmp/演示项目' }),
    );
    expect(selectDirectory).toHaveBeenCalledWith({ title: '选择项目文件夹' });
    // 挂载时拉一次，建完再拉一次。
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
  });

  it('新建项目：文件夹路径尾部分隔符被剥掉再取名', async () => {
    const { selectDirectory } = enterElectron([]);
    selectDirectory.mockResolvedValue({ canceled: false, filePaths: ['/tmp/演示目录/'] });
    createProject.mockResolvedValue({ success: true, project: makeProject() });
    renderSidebar('/agent');

    fireEvent.click(screen.getByLabelText('新建项目'));
    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith({ name: '演示目录', folderPath: '/tmp/演示目录/' }),
    );
  });

  it('新建项目：取消目录选择则不建项目', async () => {
    const { selectDirectory } = enterElectron([]);
    renderSidebar('/agent');

    fireEvent.click(screen.getByLabelText('新建项目'));
    await waitFor(() => expect(selectDirectory).toHaveBeenCalled());
    expect(createProject).not.toHaveBeenCalled();
  });

  it('内联重命名：提交新名后调 updateProject 并刷新', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    updateProject.mockResolvedValue({ success: true, project: makeProject() });
    renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('重命名项目'));
    const input = screen.getByDisplayValue('项目甲');
    fireEvent.change(input, { target: { value: '项目甲改' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(updateProject).toHaveBeenCalledWith('proj-1', { name: '项目甲改' }));
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
  });

  it('内联重命名：Escape 取消，不调 updateProject', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('重命名项目'));
    const input = screen.getByDisplayValue('项目甲');
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(screen.queryByDisplayValue('项目甲')).toBeNull();
    expect(updateProject).not.toHaveBeenCalled();
  });

  it('内联重命名：空白名直接丢弃，不调 updateProject', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('重命名项目'));
    const input = screen.getByDisplayValue('项目甲');
    fireEvent.change(input, { target: { value: '   ' } });
    fireEvent.submit(input.closest('form')!);

    expect(screen.queryByDisplayValue('项目甲')).toBeNull();
    expect(updateProject).not.toHaveBeenCalled();
  });

  it('更换绑定文件夹：选中新路径后调 updateProject', async () => {
    const { selectDirectory } = enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    selectDirectory.mockResolvedValue({ canceled: false, filePaths: ['/tmp/new-dir'] });
    updateProject.mockResolvedValue({ success: true, project: makeProject() });
    renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('更换绑定文件夹'));
    await waitFor(() =>
      expect(updateProject).toHaveBeenCalledWith('proj-1', { folderPath: '/tmp/new-dir' }),
    );
    expect(selectDirectory).toHaveBeenCalledWith({ title: '重新选择项目文件夹' });
  });

  it('删除项目两段确认：第二次点击才删除并刷新会话列表', async () => {
    enterElectron([makeProject({ id: 'proj-1', name: '项目甲' })]);
    deleteProject.mockResolvedValue({ success: true, detachedChats: 2 });
    const { data } = renderSidebar('/agent');

    await screen.findByText('项目甲');
    fireEvent.click(screen.getByTitle('删除项目（会话保留为无项目对话）'));
    expect(deleteProject).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTitle('再次点击确认删除（会话会保留为无项目对话）'));
    await waitFor(() => expect(deleteProject).toHaveBeenCalledWith('proj-1'));
    await waitFor(() => expect(data.refreshChats).toHaveBeenCalled());
  });

  it('项目列表拉取失败时错误进 alert 横幅', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    listProjects.mockReset();
    listProjects.mockRejectedValue(new Error('存储读取失败'));
    bridgeStub = baseElectronBridge();
    renderSidebar('/agent');

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('存储读取失败'));
    consoleSpy.mockRestore();
  });
});
