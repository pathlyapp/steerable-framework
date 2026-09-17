/**
 * AgentPage — 对话主页面（落地页 /agent 与会话页 /agent/:chatId）：
 *   - 落地页：输入即建会话并跳转，首条消息经 pending-first-message 接力自动发出；
 *     创建失败展示错误；浏览器预览模式禁用输入并提示；
 *   - 水合层：加载态 → 历史消息按时间正序渲染（后端返回 DESC，前端翻正）；
 *     失败时横幅提示、输入框仍可用；预览模式跳过水合；
 *   - 发送流程：真实 useChatStream + mock transport——用户消息与流式回复都渲染，
 *     流式中停止按钮经 transport.cancelActive 真正取消后端回合，错误落进消息；
 *   - W7-1 中断恢复卡（继续走 resume 通道 / 忽略仅本次挂载隐藏）；
 *   - plan 模式回合结束后的「开始执行计划」操作条；
 *   - 助手回复后的 3 条下一轮输入建议（历史 metadata 水合 / 广播到达）；
 *   - 切走再切回时用 live-stream 快照叠出远端运行中的回合。
 * 子组件自身的交互（ChatInput 排队、ChatHeader 分支菜单、ModelPicker 目录）
 * 由各自的测试覆盖，这里只验页面级接线。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import type { AgentOutletContext } from '@/layouts/AgentLayout';
import type { LocalChat, LocalChatAgent } from '@/lib/local-api';

const electronState = { active: true };
const bridgeRequest = vi.fn();
const captureScreenshot = vi.fn();
const streamMock = vi.fn();
const steerMock = vi.fn();
const cancelActiveMock = vi.fn();
let suggestedRepliesHandler:
  | ((payload: { chatId: string; messageId: string; suggestions: string[] }) => void)
  | null = null;
const regenerateChatMessage = vi.fn();
const getChatLiveStream = vi.fn();
const listProjects = vi.fn();
const listChatTasks = vi.fn();
const getLlmModels = vi.fn();
const getLlmSettings = vi.fn();
const trackBehavior = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => electronState.active,
  getElectronBridge: () =>
    electronState.active
      ? {
          runtime: 'local',
          platform: 'darwin',
          local: { captureScreenshot: (rect?: unknown) => captureScreenshot(rect) },
          localBackend: {
            request: (input: { method: string; path: string; body?: unknown }) =>
              bridgeRequest(input),
            startStream: vi.fn(async () => null),
            cancelStream: vi.fn(),
            steerChat: (_chatId: string, content: string) => steerMock(content),
          },
          onSuggestedReplies: (callback: (payload: {
            chatId: string;
            messageId: string;
            suggestions: string[];
          }) => void) => {
            suggestedRepliesHandler = callback;
            return () => {
              if (suggestedRepliesHandler === callback) suggestedRepliesHandler = null;
            };
          },
        }
      : null,
}));

vi.mock('@/lib/local-api', () => ({
  getChatLiveStream: (chatId: string) => getChatLiveStream(chatId),
  listProjects: () => listProjects(),
  listChatTasks: (chatId: string) => listChatTasks(chatId),
  getLlmModels: (draft?: unknown) => getLlmModels(draft),
  getLlmSettings: () => getLlmSettings(),
  setLlmSettings: vi.fn(async (s: unknown) => s),
  getCompatFlags: vi.fn(async () => ({ flags: [] })),
  getProviderPresets: vi.fn(async () => ({ presets: [] })),
  getCatalogProviders: vi.fn(async () => ({ providers: [] })),
  resolveProviderPreset: vi.fn(async () => ({ preset: null })),
  getChatProjectContext: vi.fn(async () => ({ project: null, ruleFileCount: 0 })),
  setProjectTrusted: vi.fn(async () => ({ success: true })),
  updateChatProject: vi.fn(async () => ({ id: 'chat-1', projectId: null })),
  updateProject: vi.fn(async () => ({ success: true })),
  getChatBranches: vi.fn(async () => ({ activeRecordId: 'r1', lineage: [], children: [] })),
  activateChatBranch: vi.fn(async () => ({ activeRecordId: 'r1', messageCount: 0 })),
  getChatBranchTree: vi.fn(async () => ({
    activeRecordId: 'r1',
    tree: null,
    nodeCount: 0,
    truncated: false,
  })),
  mergeTaskWorktree: vi.fn(),
  discardTaskWorktree: vi.fn(),
}));

vi.mock('@/lib/chat-transport', () => ({
  createElectronChatTransport: () => ({
    stream: (
      input: { content: string; metadata?: Record<string, unknown> },
      onEvent: (event: SSEEvent) => void,
    ) => streamMock(input, onEvent),
    steer: (content: string) => steerMock(content),
    cancelActive: () => cancelActiveMock(),
  }),
  regenerateChatMessage: (chatId: string, messageId: string) =>
    regenerateChatMessage(chatId, messageId),
}));

vi.mock('@/lib/insights', () => ({
  trackBehavior: (name: string, props?: Record<string, unknown>) => trackBehavior(name, props),
}));

const { AgentPage } = await import('./AgentPage');

const AGENT: LocalChatAgent = {
  id: 'local-assistant',
  slug: 'local-assistant',
  name: '本地助手',
  icon: null,
  color: '#4f46e5',
  description: null,
  rolePrompt: null,
  isBuiltin: true,
};

const CHAT: LocalChat = {
  id: 'chat-1',
  projectId: null,
  userId: 'local',
  title: '示例对话',
  agentId: AGENT.id,
  createdAt: '2026-09-01T08:00:00.000Z',
  updatedAt: '2026-09-01T08:05:00.000Z',
  isPinned: false,
  systemPrompt: null,
  pinnedRefs: null,
};

function makeCtx(overrides: Partial<AgentOutletContext> = {}): AgentOutletContext {
  return {
    chats: [CHAT],
    agents: [AGENT],
    isLoading: false,
    error: null,
    selectedAgentId: AGENT.id,
    setSelectedAgentId: vi.fn(),
    refreshChats: vi.fn(async () => {}),
    refreshAgents: vi.fn(async () => {}),
    createChat: vi.fn(async () => 'chat-new'),
    deleteChat: vi.fn(async () => true),
    patchChatTitle: vi.fn(),
    isLoadingMoreChats: false,
    hasMoreChats: false,
    loadMoreChats: vi.fn(async () => {}),
    registerChatMessageSender: vi.fn(),
    sendChatMessage: vi.fn(),
    inspectTask: vi.fn(),
    ...overrides,
  };
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname}{loc.search}</div>;
}

function renderPage(entry: string, ctx: AgentOutletContext) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          element={
            <>
              <Outlet context={ctx} />
              <LocationProbe />
            </>
          }
        >
          <Route path="/agent" element={<AgentPage />} />
          <Route path="/agent/:chatId" element={<AgentPage />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

/** 历史消息水合的默认应答；各用例可按路径覆盖。 */
function defaultBridgeRequest(input: { method: string; path: string }): Promise<unknown> {
  if (input.path.includes('/messages')) {
    return Promise.resolve({ messages: [], interrupted: false });
  }
  if (input.path.endsWith('/chat-agents/skills')) return Promise.resolve({ skills: [] });
  if (input.path.endsWith('/chat-agents/mcp-tools')) return Promise.resolve({ mcpTools: [] });
  return Promise.resolve({});
}

/**
 * 模拟真实 transport 的事件序列（见 lib/chat-transport.ts 的 LocalBackendSseAdapter）：
 * 每个 content 增量都会同步合成一条 turn_timeline 事件，助手气泡的正文靠它渲染。
 */
function emitTextReply(onEvent: (event: SSEEvent) => void, text: string) {
  onEvent({ type: 'content', content: text });
  onEvent({
    type: 'agent',
    event: 'turn_timeline',
    payload: { blocks: [{ type: 'text', content: text }] },
  });
  onEvent({ type: 'done' });
}

/**
 * 往输入框键入文本并等受控值同步完成（发送按钮可用即同步完成）。
 * 必须先 focus：ChatInput 的布局副作用只在编辑器聚焦时才跳过重写 DOM，
 * 不聚焦时任何重渲染都会把键入内容清掉（真实用户打字时编辑器必然聚焦）。
 *
 * 全量并行跑时，水合/skills/直播快照等异步应答落地触发的重渲染可能恰好
 * 打在 fireEvent.input 与 setTimeout(0) 同步之间，把 textContent 重置回
 * 受控空值——键入就此丢失。真实用户遇到会重打，测试也按重试建模：
 * 最多 3 次，消除这类时序抖动。
 */
async function typeComposer(text: string) {
  const editor = screen.getByRole('textbox') as HTMLElement;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    editor.focus();
    editor.textContent = text;
    fireEvent.input(editor);
    // 编辑器同步经 setTimeout(0) 推受控值；全量并行跑时定时器调度较慢，给足轮询预算。
    const synced = await waitFor(
      () => {
        const send = screen.getByTestId('chat-send') as HTMLButtonElement;
        expect(send.disabled).toBe(false);
      },
      { timeout: 2000 },
    )
      .then(() => true)
      .catch(() => false);
    if (synced) return;
  }
  // 三次都被重渲染清掉：按最终状态断言失败，把 DOM 打出来便于诊断。
  const send = screen.getByTestId('chat-send') as HTMLButtonElement;
  expect(send.disabled).toBe(false);
}

/** 预览模式（输入禁用）下键入：发送按钮永不放行，只等一个事件循环让同步落地。 */
async function typeComposerDisabled(text: string) {
  const editor = screen.getByRole('textbox') as HTMLElement;
  editor.focus();
  editor.textContent = text;
  fireEvent.input(editor);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
}

function pressEnter() {
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  electronState.active = true;
  suggestedRepliesHandler = null;
  bridgeRequest.mockImplementation(defaultBridgeRequest);
  getChatLiveStream.mockResolvedValue({ active: false });
  listProjects.mockResolvedValue({ projects: [] });
  listChatTasks.mockResolvedValue({ tasks: [] });
  getLlmModels.mockResolvedValue({ models: [], catalogStatus: 'offline' });
  getLlmSettings.mockResolvedValue({ provider: 'openai-compat', model: 'acme-chat' });
  streamMock.mockImplementation(
    async (_input: unknown, onEvent: (event: SSEEvent) => void) => {
      emitTextReply(onEvent, '默认回复');
    },
  );
  steerMock.mockResolvedValue(true);
});

afterEach(cleanup);

describe('AgentPage 落地页（EmptyChatGate）', () => {
  it('无 chatId 时渲染落地页：品牌标题、副标题与输入框', async () => {
    renderPage('/agent', makeCtx());
    await screen.findByTestId('empty-chat-home');
    expect(screen.getByText('Steerable Shell')).toBeTruthy();
    expect(screen.getByText('输入消息，直接开始一段新对话。')).toBeTruthy();
    expect(screen.getByRole('textbox')).toBeTruthy();
  });

  it('输入消息：创建会话、暂存首条消息并跳转，新页面自动接力发出', async () => {
    const ctx = makeCtx();
    renderPage('/agent', ctx);
    await screen.findByTestId('empty-chat-home');
    await typeComposer('你好，帮我看看日志');
    pressEnter();

    await waitFor(() =>
      expect(ctx.createChat).toHaveBeenCalledWith(expect.objectContaining({ agentId: AGENT.id })),
    );
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/agent/chat-new'));
    expect(trackBehavior).toHaveBeenCalledWith(
      'composer_send',
      expect.objectContaining({ home: true, mode: 'agent' }),
    );
    // 接力：新会话视图挂载后自动把暂存的首条消息发出去。
    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({ content: '你好，帮我看看日志' }),
        expect.any(Function),
      ),
    );
    expect(await screen.findByText('默认回复')).toBeTruthy();
  });

  it('空输入不创建会话', async () => {
    const ctx = makeCtx();
    renderPage('/agent', ctx);
    await screen.findByTestId('empty-chat-home');
    pressEnter();
    expect(ctx.createChat).not.toHaveBeenCalled();
  });

  it('创建会话失败时展示错误并恢复可交互', async () => {
    const ctx = makeCtx({
      createChat: vi.fn(async () => {
        throw new Error('磁盘已满');
      }),
    });
    renderPage('/agent', ctx);
    await screen.findByTestId('empty-chat-home');
    await typeComposer('hello');
    pressEnter();
    expect((await screen.findByRole('alert')).textContent).toContain('磁盘已满');
    expect(screen.queryByText('正在创建对话…')).toBeNull();
    // 未跳转
    expect(screen.getByTestId('loc').textContent).toBe('/agent');
  });

  it('创建会话返回空 id 时提示重试', async () => {
    const ctx = makeCtx({ createChat: vi.fn(async () => null) });
    renderPage('/agent', ctx);
    await screen.findByTestId('empty-chat-home');
    await typeComposer('hello');
    pressEnter();
    expect((await screen.findByRole('alert')).textContent).toContain('创建对话失败，请重试');
  });

  it('带 ?projectId= 进入时创建会话携带项目', async () => {
    const ctx = makeCtx();
    renderPage('/agent?projectId=proj-9', ctx);
    await screen.findByTestId('empty-chat-home');
    await typeComposer('看下构建');
    pressEnter();
    await waitFor(() =>
      expect(ctx.createChat).toHaveBeenCalledWith(expect.objectContaining({ projectId: 'proj-9' })),
    );
  });

  it('浏览器预览模式提示无 IPC 桥接，输入不创建会话', async () => {
    electronState.active = false;
    const ctx = makeCtx();
    renderPage('/agent', ctx);
    await screen.findByTestId('empty-chat-home');
    expect(screen.getByText(/浏览器预览模式/)).toBeTruthy();
    await typeComposerDisabled('hello');
    pressEnter();
    expect(ctx.createChat).not.toHaveBeenCalled();
  });
});

describe('AgentPage 水合层（AgentChatLoader）', () => {
  it('水合期间显示加载态，完成后按时间正序渲染历史消息', async () => {
    let resolveMessages: (value: unknown) => void = () => {};
    bridgeRequest.mockImplementation((input: { method: string; path: string }) => {
      if (input.path.includes('/messages')) {
        return new Promise((resolve) => {
          resolveMessages = resolve;
        });
      }
      return defaultBridgeRequest(input);
    });
    renderPage('/agent/chat-1', makeCtx());
    expect(await screen.findByText('加载对话历史…')).toBeTruthy();

    // 后端按 createdAt DESC 返回（最新在前），页面应翻正为 ASC。
    resolveMessages({
      messages: [
        {
          id: 'm2',
          chatId: 'chat-1',
          role: 'assistant',
          content: '助手回复',
          createdAt: '2026-09-01T08:01:00.000Z',
        },
        {
          id: 'm1',
          chatId: 'chat-1',
          role: 'user',
          content: '用户提问',
          createdAt: '2026-09-01T08:00:00.000Z',
        },
      ],
      interrupted: false,
    });
    const userBubble = await screen.findByText('用户提问');
    const assistantBubble = await screen.findByText('助手回复');
    expect(
      userBubble.compareDocumentPosition(assistantBubble) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // 水合请求打在会话消息端点上
    expect(
      bridgeRequest.mock.calls.some(([input]) =>
        String(input.path).includes('/api/v2/chats/chat-1/messages'),
      ),
    ).toBe(true);
  });

  it('历史消息加载失败时显示错误横幅，输入框仍可用', async () => {
    bridgeRequest.mockImplementation((input: { method: string; path: string }) => {
      if (input.path.includes('/messages')) return Promise.reject(new Error('数据库锁定'));
      return defaultBridgeRequest(input);
    });
    renderPage('/agent/chat-1', makeCtx());
    expect((await screen.findByText(/加载对话历史失败/)).textContent).toContain('数据库锁定');
    expect(screen.getByRole('textbox')).toBeTruthy();
  });

  it('浏览器预览模式跳过水合直接渲染输入框', async () => {
    electronState.active = false;
    renderPage('/agent/chat-1', makeCtx());
    expect(await screen.findByRole('textbox')).toBeTruthy();
    expect(screen.queryByText('加载对话历史…')).toBeNull();
    expect(bridgeRequest).not.toHaveBeenCalled();
  });
});

describe('AgentPage 发送流程', () => {
  it('发送消息：transport 收到内容，用户消息与流式回复都渲染', async () => {
    streamMock.mockImplementation(
      async (_input: unknown, onEvent: (event: SSEEvent) => void) => {
        emitTextReply(onEvent, '这是回复');
      },
    );
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    await typeComposer('你好');
    pressEnter();

    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({ content: '你好' }),
        expect.any(Function),
      ),
    );
    expect(await screen.findByText('这是回复')).toBeTruthy();
    // 用户气泡落地（输入框已清空，剩下的文本节点即消息）
    expect(screen.getByText('你好')).toBeTruthy();
    expect(trackBehavior).toHaveBeenCalledWith(
      'composer_send',
      expect.objectContaining({ mode: 'agent', empty: false }),
    );
  });

  it('流式中显示停止按钮，点击经 cancelActive 取消后端回合', async () => {
    let finishStream: () => void = () => {};
    streamMock.mockImplementation((_input: unknown, onEvent: (event: SSEEvent) => void) => {
      onEvent({ type: 'content', content: '部分回复' });
      onEvent({
        type: 'agent',
        event: 'turn_timeline',
        payload: { blocks: [{ type: 'text', content: '部分回复' }] },
      });
      return new Promise<void>((resolve) => {
        finishStream = resolve;
      });
    });
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    await typeComposer('讲个故事');
    pressEnter();

    const stop = await screen.findByRole('button', { name: '停止生成' });
    expect(await screen.findByText('部分回复')).toBeTruthy();
    fireEvent.click(stop);
    expect(cancelActiveMock).toHaveBeenCalled();
    // 前端状态复位：停止按钮回到发送按钮
    await screen.findByRole('button', { name: '发送消息' });
    await act(async () => {
      finishStream();
    });
  });

  it('流式传输出错时错误落进助手消息', async () => {
    streamMock.mockImplementation(async () => {
      throw new Error('网关超时');
    });
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    await typeComposer('你好');
    pressEnter();
    // useChatStream 把错误打成 "[stream error] ..." 内容，AssistantMessage 的
    // readTurnFailure 剥掉前缀后渲染成错误气泡「请求失败：…」。
    expect((await screen.findByText(/请求失败/)).textContent).toContain('网关超时');
  });

  it('挂载时向布局注册发送器，卸载时注销', async () => {
    const ctx = makeCtx();
    const { unmount } = renderPage('/agent/chat-1', ctx);
    await screen.findByRole('textbox');
    expect(ctx.registerChatMessageSender).toHaveBeenCalledWith(expect.any(Function));
    unmount();
    expect(ctx.registerChatMessageSender).toHaveBeenLastCalledWith(null);
  });

  it('点击分享调用主进程截图', async () => {
    captureScreenshot.mockResolvedValue({ success: true, width: 100, height: 80 });
    bridgeRequest.mockImplementation((input: { method: string; path: string }) => {
      if (input.path.includes('/messages')) {
        return Promise.resolve({
          messages: [
            {
              id: 'm1',
              chatId: 'chat-1',
              role: 'assistant',
              content: '历史回复',
              createdAt: '2026-09-01T08:00:00.000Z',
            },
          ],
          interrupted: false,
        });
      }
      return defaultBridgeRequest(input);
    });
    renderPage('/agent/chat-1', makeCtx());
    fireEvent.click(await screen.findByRole('button', { name: '分享对话截图' }));
    await waitFor(() => expect(captureScreenshot).toHaveBeenCalled());
  });
});

describe('AgentPage 中断恢复卡（W7-1）', () => {
  function hydrateInterrupted() {
    bridgeRequest.mockImplementation((input: { method: string; path: string }) => {
      if (input.path.includes('/messages')) {
        return Promise.resolve({
          messages: [
            {
              id: 'm1',
              chatId: 'chat-1',
              role: 'user',
              content: '之前的问题',
              createdAt: '2026-09-01T08:00:00.000Z',
            },
          ],
          interrupted: true,
        });
      }
      return defaultBridgeRequest(input);
    });
  }

  it('上一轮中断时展示恢复卡，「继续上次回复」走 resume 通道', async () => {
    hydrateInterrupted();
    renderPage('/agent/chat-1', makeCtx());
    fireEvent.click(await screen.findByRole('button', { name: '继续上次回复' }));
    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '',
          metadata: expect.objectContaining({ resume: true }),
        }),
        expect.any(Function),
      ),
    );
    // resume 不追加用户消息：列表里仍只有「之前的问题」一条用户消息
    expect(screen.getAllByText('之前的问题')).toHaveLength(1);
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '继续上次回复' })).toBeNull(),
    );
  });

  it('「忽略」仅本次挂载隐藏中断卡，不发起 resume', async () => {
    hydrateInterrupted();
    renderPage('/agent/chat-1', makeCtx());
    fireEvent.click(await screen.findByRole('button', { name: '忽略' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '继续上次回复' })).toBeNull(),
    );
    expect(streamMock).not.toHaveBeenCalled();
  });
});

describe('AgentPage plan 模式操作条', () => {
  it('plan 回合结束后展示「开始执行计划」，点击切回 agent 并自动发执行指令', async () => {
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    fireEvent.click(screen.getByTestId('mode-plan'));
    await typeComposer('先出个计划');
    pressEnter();

    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({ metadata: expect.objectContaining({ mode: 'plan' }) }),
        expect.any(Function),
      ),
    );
    const execute = await screen.findByTestId('plan-execute');
    fireEvent.click(execute);
    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({ content: '请按照上面的计划开始执行。' }),
        expect.any(Function),
      ),
    );
    await waitFor(() => expect(screen.queryByTestId('plan-execute')).toBeNull());
  });

  it('「忽略」收起操作条且不再发送', async () => {
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    fireEvent.click(screen.getByTestId('mode-plan'));
    await typeComposer('先出个计划');
    pressEnter();
    await screen.findByTestId('plan-execute');
    streamMock.mockClear();
    fireEvent.click(screen.getByTestId('plan-dismiss'));
    await waitFor(() => expect(screen.queryByTestId('plan-execute')).toBeNull());
    expect(streamMock).not.toHaveBeenCalled();
  });
});

describe('AgentPage 远端回合恢复', () => {
  it('切回时远端回合仍在运行：叠加显示运行中的部分内容', async () => {
    getChatLiveStream.mockResolvedValue({
      active: true,
      status: 'running',
      content: '远端正在输出',
    });
    renderPage('/agent/chat-1', makeCtx());
    expect(await screen.findByText('远端正在输出')).toBeTruthy();
    // 远端运行中等同流式：输入区显示停止按钮
    expect(await screen.findByRole('button', { name: '停止生成' })).toBeTruthy();
  });
});

describe('AgentPage 追问建议', () => {
  it('历史助手消息带 suggestedReplies 时渲染芯片，点击即发出', async () => {
    bridgeRequest.mockImplementation((input: { method: string; path: string }) => {
      if (input.path.includes('/messages')) {
        return Promise.resolve({
          messages: [
            {
              id: 'm2',
              chatId: 'chat-1',
              role: 'assistant',
              content: 'PPT 已生成。如需修改内容或调整样式，请告诉我。',
              createdAt: '2026-09-17T08:01:00.000Z',
              messageMetadata: JSON.stringify({
                suggestedReplies: ['调整封面配色', '把个人简介写得更具体', '再加一页项目案例'],
              }),
            },
            {
              id: 'm1',
              chatId: 'chat-1',
              role: 'user',
              content: '制作自我介绍ppt',
              createdAt: '2026-09-17T08:00:00.000Z',
            },
          ],
          interrupted: false,
        });
      }
      return defaultBridgeRequest(input);
    });
    renderPage('/agent/chat-1', makeCtx());
    expect(await screen.findByTestId('suggested-replies')).toBeTruthy();
    fireEvent.click(screen.getByText('调整封面配色'));
    await waitFor(() =>
      expect(streamMock).toHaveBeenCalledWith(
        expect.objectContaining({ content: '调整封面配色' }),
        expect.any(Function),
      ),
    );
  });

  it('回合结束后收到 suggested-replies 广播则画出芯片', async () => {
    streamMock.mockImplementation(
      async (_input: unknown, onEvent: (event: SSEEvent) => void) => {
        onEvent({ type: 'content', content: 'PPT 已生成' });
        onEvent({
          type: 'agent',
          event: 'turn_timeline',
          payload: { blocks: [{ type: 'text', content: 'PPT 已生成' }] },
        });
        onEvent({
          type: 'agent',
          event: 'message_id',
          payload: { messageId: 'asst-db-1' },
        });
        onEvent({ type: 'done' });
      },
    );
    renderPage('/agent/chat-1', makeCtx());
    await screen.findByRole('textbox');
    await typeComposer('做个 ppt');
    pressEnter();
    await screen.findByText('PPT 已生成');
    await waitFor(() => expect(suggestedRepliesHandler).toBeTruthy());
    act(() => {
      suggestedRepliesHandler?.({
        chatId: 'chat-1',
        messageId: 'asst-db-1',
        suggestions: ['调整封面配色', '把个人简介写得更具体', '再加一页项目案例'],
      });
    });
    expect(await screen.findByText('调整封面配色')).toBeTruthy();
  });
});
