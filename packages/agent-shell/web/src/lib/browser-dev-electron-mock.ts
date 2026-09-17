import type {
  ElectronBridge,
  LocalBackendRequestInput,
  LocalBackendStreamEvent,
} from './electron-bridge';
import type {
  ChatAgentWriteInput,
  LocalChat,
  LocalChatAgent,
  LocalChatMessage,
  LlmSettings,
} from './local-api';
import { pickDefaultAgentId } from '@/brand';
import { isDemoMode } from './demo-flag';
import browserDevData from '../fixtures/browser-dev-data.json';

type MutableChat = LocalChat;
type MutableMessage = LocalChatMessage;

interface BrowserDevFixture {
  version?: number;
  exportedAt?: string | null;
  source?: string | null;
  agents?: LocalChatAgent[];
  chats?: MutableChat[];
  messagesByChatId?: Record<string, MutableMessage[]>;
  llmSettings?: LlmSettings | null;
  skills?: Array<{ name: string; description: string; builtin?: boolean }>;
}

interface BrowserMockState {
  agents: LocalChatAgent[];
  chats: MutableChat[];
  messagesByChatId: Record<string, MutableMessage[]>;
  llmSettings: LlmSettings;
  skills: Array<{ name: string; description: string; builtin?: boolean }>;
  activeStreams: Map<string, number[]>;
  source: 'fixture' | 'fallback';
  fixtureMeta: Pick<BrowserDevFixture, 'exportedAt' | 'source'> | null;
}

const suggestedReplyListeners = new Set<
  (payload: { chatId: string; messageId: string; suggestions: string[] }) => void
>();

const USER_ID = 'browser-preview-user';
const now = new Date();

function iso(minutesAgo: number): string {
  return new Date(now.getTime() - minutesAgo * 60_000).toISOString();
}

function createChatRecord(input: {
  id: string;
  title: string;
  agentId: string | null;
  minutesAgo: number;
  isPinned?: boolean;
}): MutableChat {
  return {
    id: input.id,
    projectId: null,
    userId: USER_ID,
    title: input.title,
    agentId: input.agentId,
    createdAt: iso(input.minutesAgo + 10),
    updatedAt: iso(input.minutesAgo),
    isPinned: Boolean(input.isPinned),
    systemPrompt: null,
    pinnedRefs: null,
  };
}

function createMessage(input: {
  id: string;
  chatId: string;
  role: MutableMessage['role'];
  content: string;
  minutesAgo: number;
  messageMetadata?: string | null;
}): MutableMessage {
  return {
    id: input.id,
    chatId: input.chatId,
    role: input.role,
    content: input.content,
    createdAt: iso(input.minutesAgo),
    messageMetadata: input.messageMetadata ?? null,
  };
}

function createInitialState(): BrowserMockState {
  const fixtureState = createStateFromFixture(browserDevData as BrowserDevFixture);
  if (fixtureState) return fixtureState;

  const agents: LocalChatAgent[] = [
    {
      id: 'coach',
      slug: 'coach',
      name: '行动教练',
      icon: null,
      color: '#2563eb',
      description: '把目标拆成可执行计划，适合调试标准聊天路径。',
      rolePrompt: '你是一个行动教练。',
      isBuiltin: true,
      sortOrder: 1,
    },
    {
      id: 'tool-demo',
      slug: 'tool-demo',
      name: '工具演示专家',
      icon: null,
      color: '#7c3aed',
      description: '模拟本地工具调用和执行摘要，适合调试工具卡片。',
      rolePrompt: '你是工具演示智能体。',
      isBuiltin: true,
      sortOrder: 2,
    },
    {
      id: 'writer',
      slug: 'writer',
      name: '文案顾问',
      icon: null,
      color: '#16a34a',
      description: '偏长文本回复样例，适合调试 markdown 与滚动。',
      rolePrompt: '你是一个文案顾问。',
      isBuiltin: true,
      sortOrder: 3,
    },
  ];

  const chats = [
    createChatRecord({
      id: 'chat-browser-demo',
      title: '浏览器 UI 调试样例',
      agentId: 'coach',
      minutesAgo: 8,
      isPinned: true,
    }),
    createChatRecord({
      id: 'chat-tool-demo',
      title: '工具调用卡片预览',
      agentId: 'tool-demo',
      minutesAgo: 64,
    }),
  ];

  return {
    agents,
    chats,
    messagesByChatId: {
      'chat-browser-demo': [
        createMessage({
          id: 'msg-demo-assistant-1',
          chatId: 'chat-browser-demo',
          role: 'assistant',
          content:
            '可以在这里调试普通浏览器里的聊天 UI。这个回复来自 dev mock bridge，不依赖 Electron IPC。',
          minutesAgo: 7,
        }),
        createMessage({
          id: 'msg-demo-user-1',
          chatId: 'chat-browser-demo',
          role: 'user',
          content: '我想在浏览器里调试聊天界面。',
          minutesAgo: 8,
        }),
      ],
      'chat-tool-demo': [
        createMessage({
          id: 'msg-tool-assistant-1',
          chatId: 'chat-tool-demo',
          role: 'assistant',
          content: '我已经模拟执行了一次本地命令，并把结果展示在工具卡片里。',
          minutesAgo: 63,
          messageMetadata: JSON.stringify({
            executedActions: [
              {
                tool: 'local_exec',
                arguments: { command: 'tool_demo list --limit 5' },
                result: { success: true, output: 'Found 5 mock records.' },
              },
            ],
          }),
        }),
        createMessage({
          id: 'msg-tool-user-1',
          chatId: 'chat-tool-demo',
          role: 'user',
          content: '帮我看一下最近的工具调用记录。',
          minutesAgo: 64,
        }),
      ],
    },
    llmSettings: {
      provider: 'ollama',
      model: 'browser-preview-model',
      baseUrl: 'http://127.0.0.1:11434',
      apiKey: '',
      temperature: 0.3,
      maxTotalTokens: 60_000,
    },
    skills: [
      { name: 'tool-demo', description: '模拟查询与回放工具。', builtin: true },
      { name: 'local-exec', description: '模拟本地命令执行。', builtin: true },
      { name: 'anti-deferred', description: '模拟防拖延执行策略。', builtin: true },
    ],
    activeStreams: new Map(),
    source: 'fallback',
    fixtureMeta: null,
  };
}

const state = createInitialState();

function createStateFromFixture(fixture: BrowserDevFixture): BrowserMockState | null {
  const agents = Array.isArray(fixture.agents) ? fixture.agents : [];
  const chats = Array.isArray(fixture.chats) ? fixture.chats : [];
  if (agents.length === 0 || chats.length === 0) return null;

  const messagesByChatId = fixture.messagesByChatId ?? {};
  return {
    agents,
    chats,
    messagesByChatId,
    llmSettings: fixture.llmSettings ?? {
      provider: 'ollama',
      model: 'browser-preview-model',
      baseUrl: 'http://127.0.0.1:11434',
      apiKey: '',
      temperature: 0.3,
      maxTotalTokens: 60_000,
    },
    skills:
      Array.isArray(fixture.skills) && fixture.skills.length > 0
        ? fixture.skills
        : [
            { name: 'tool-demo', description: '查询与回放工具。', builtin: true },
            { name: 'local-exec', description: '本地命令执行。', builtin: true },
          ],
    activeStreams: new Map(),
    source: 'fixture',
    fixtureMeta: {
      exportedAt: fixture.exportedAt ?? null,
      source: fixture.source ?? null,
    },
  };
}

function paginateChats(page: number, limit: number) {
  const sorted = [...state.chats].sort((a, b) => {
    if (a.isPinned !== b.isPinned) return a.isPinned ? -1 : 1;
    return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
  });
  const start = (page - 1) * limit;
  const chats = sorted.slice(start, start + limit);
  return {
    chats,
    pagination: {
      page,
      limit,
      total: sorted.length,
      totalPages: Math.max(1, Math.ceil(sorted.length / limit)),
      hasMore: start + chats.length < sorted.length,
    },
  };
}

function parseQuery(path: string): URLSearchParams {
  const queryStart = path.indexOf('?');
  return new URLSearchParams(queryStart >= 0 ? path.slice(queryStart + 1) : '');
}

function stripQuery(path: string): string {
  const queryStart = path.indexOf('?');
  return queryStart >= 0 ? path.slice(0, queryStart) : path;
}

function getChatIdFromPath(path: string, suffix = ''): string | null {
  const cleanPath = stripQuery(path);
  const match = cleanPath.match(/^\/api\/v2\/chats\/([^/]+)(?:\/.*)?$/);
  if (!match) return null;
  if (suffix && !cleanPath.endsWith(suffix)) return null;
  return decodeURIComponent(match[1]);
}

function updateChatTimestamp(chatId: string) {
  const chat = state.chats.find((item) => item.id === chatId);
  if (chat) chat.updatedAt = new Date().toISOString();
}

async function request<T>(input: LocalBackendRequestInput): Promise<T> {
  const method = input.method.toUpperCase();
  const path = input.path;
  const cleanPath = stripQuery(path);

  if (method === 'GET' && cleanPath === '/api/v2/chats') {
    const query = parseQuery(path);
    const page = Number(query.get('page') ?? '1');
    const limit = Number(query.get('limit') ?? '50');
    return paginateChats(page, limit) as T;
  }

  if (method === 'GET' && cleanPath === '/api/v2/chat-agents') {
    const includeArchived = parseQuery(path).get('include_archived') === 'true';
    const agents = includeArchived
      ? state.agents
      : state.agents.filter((agent) => !agent.isArchived);
    return { agents, total: agents.length } as T;
  }

  if (method === 'POST' && cleanPath === '/api/v2/chat-agents') {
    const body = (input.body ?? {}) as Partial<ChatAgentWriteInput>;
    const id =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `agent-browser-${Date.now()}`;
    const agent: LocalChatAgent = {
      id,
      slug: null,
      name: (body.name || '新助手').trim() || '新助手',
      icon: null,
      color: body.color ?? '#4f46e5',
      description: body.description ?? null,
      rolePrompt: body.rolePrompt ?? null,
      skillIds: body.skillIds ?? [],
      toolPolicy: body.toolPolicy ?? { mode: 'all', tools: [] },
      allowExternalSkills: body.allowExternalSkills ?? true,
      loadAllSkills: body.loadAllSkills ?? false,
      isBuiltin: false,
      isArchived: false,
      sortOrder: body.sortOrder ?? state.agents.length,
    };
    state.agents.push(agent);
    return { agent } as T;
  }

  const chatAgentMatch = cleanPath.match(/^\/api\/v2\/chat-agents\/([^/]+)$/);
  if (chatAgentMatch) {
    const agentId = decodeURIComponent(chatAgentMatch[1]);
    const reserved = new Set(['skills', 'mcp-tools', 'templates', 'tools', 'generate']);
    if (!reserved.has(agentId)) {
      const index = state.agents.findIndex((agent) => agent.id === agentId);
      if (index < 0) {
        throw new Error(`Agent not found: ${agentId}`);
      }
      if (method === 'PATCH') {
        const body = (input.body ?? {}) as Partial<LocalChatAgent>;
        const current = state.agents[index];
        const updated: LocalChatAgent = {
          ...current,
          name: typeof body.name === 'string' ? body.name : current.name,
          color: body.color === undefined ? current.color : body.color,
          description:
            body.description === undefined ? current.description : body.description,
          rolePrompt:
            body.rolePrompt === undefined ? current.rolePrompt : body.rolePrompt,
          skillIds: body.skillIds === undefined ? current.skillIds : body.skillIds,
          toolPolicy:
            body.toolPolicy === undefined ? current.toolPolicy : body.toolPolicy,
          allowExternalSkills:
            body.allowExternalSkills === undefined
              ? current.allowExternalSkills
              : body.allowExternalSkills,
          loadAllSkills:
            body.loadAllSkills === undefined ? current.loadAllSkills : body.loadAllSkills,
        };
        state.agents[index] = updated;
        return { agent: updated } as T;
      }
      if (method === 'DELETE') {
        state.agents[index] = { ...state.agents[index], isArchived: true };
        return { id: agentId, status: 'archived' } as T;
      }
    }
  }

  if (method === 'GET' && cleanPath === '/api/v2/chat-agents/skills') {
    // fixture 只存 name/description；智能体的技能勾选还要 id 与 layer，
    // 这里按真实接口的字段补齐（内置技能视作常驻层）。
    return {
      skills: state.skills.map((skill) => ({
        ...skill,
        id: skill.name,
        displayName: '',
        layer: skill.builtin ? 'eager' : 'catalog',
        origin: skill.builtin ? 'builtin' : 'user',
      })),
    } as T;
  }

  if (method === 'GET' && cleanPath === '/api/v2/chat-agents/tools') {
    return {
      tools: [
        { name: 'local_exec_shell', description: '执行本地 shell 命令。', category: 'local' },
        { name: 'local_read_file', description: '读取本地文本文件。', category: 'local' },
        { name: 'local_write_file', description: '写入本地文件。', category: 'local' },
        { name: 'web_search', description: '联网搜索。', category: 'local' },
      ],
    } as T;
  }

  if (method === 'POST' && cleanPath === '/api/v2/chat-agents/skills/import') {
    const body = input.body as { path?: string } | undefined;
    const name = body?.path?.split(/[\\/]/).filter(Boolean).pop() || 'browser-skill';
    if (!state.skills.some((skill) => skill.name === name)) {
      state.skills.push({ name, description: '浏览器预览中导入的 mock skill。' });
    }
    return { success: true, name } as T;
  }

  if (method === 'DELETE' && cleanPath.startsWith('/api/v2/chat-agents/skills/delete/')) {
    const name = decodeURIComponent(cleanPath.split('/').pop() || '');
    state.skills = state.skills.filter((skill) => skill.name !== name || skill.builtin);
    return { success: true } as T;
  }

  if (method === 'GET' && cleanPath === '/api/v2/local-settings/llm') {
    return state.llmSettings as T;
  }

  if (method === 'POST' && cleanPath === '/api/v2/local-settings/llm') {
    state.llmSettings = { ...state.llmSettings, ...(input.body as Partial<LlmSettings>) };
    return state.llmSettings as T;
  }

  if (method === 'POST' && cleanPath === '/api/v2/chats/prune-empty') {
    const body = input.body as { exceptChatId?: string | null } | undefined;
    const except = body?.exceptChatId ?? null;
    const deletedChatIds: string[] = [];
    for (const chat of [...state.chats]) {
      if (except && chat.id === except) continue;
      const messages = state.messagesByChatId[chat.id] ?? [];
      if (messages.length === 0) deletedChatIds.push(chat.id);
    }
    state.chats = state.chats.filter((chat) => !deletedChatIds.includes(chat.id));
    for (const id of deletedChatIds) delete state.messagesByChatId[id];
    return { deletedChatIds } as T;
  }

  if (method === 'POST' && cleanPath === '/api/v2/chats/new') {
    const body = input.body as { agentId?: string } | undefined;
    const agentId = body?.agentId || pickDefaultAgentId(state.agents) || null;
    const chatId = `chat-browser-${Date.now()}`;
    const chat = createChatRecord({
      id: chatId,
      title: '新会话',
      agentId,
      minutesAgo: 0,
    });
    chat.createdAt = new Date().toISOString();
    chat.updatedAt = chat.createdAt;
    state.chats.unshift(chat);
    state.messagesByChatId[chatId] = [];
    return {
      success: true,
      chatId,
      projectId: null,
      isTemporary: false,
    } as T;
  }

  const messagesChatId = getChatIdFromPath(path, '/messages');
  if (method === 'GET' && messagesChatId) {
    const messages = state.messagesByChatId[messagesChatId] ?? [];
    return { messages: [...messages].sort((a, b) => b.createdAt.localeCompare(a.createdAt)) } as T;
  }

  const deleteChatId = getChatIdFromPath(path);
  if (method === 'DELETE' && deleteChatId) {
    const onlyIfEmpty = parseQuery(path).get('onlyIfEmpty') === '1';
    if (onlyIfEmpty) {
      const messages = state.messagesByChatId[deleteChatId] ?? [];
      const exists = state.chats.some((chat) => chat.id === deleteChatId);
      if (!exists || messages.length > 0) {
        return { success: true, deleted: false, chatId: deleteChatId } as T;
      }
      state.chats = state.chats.filter((chat) => chat.id !== deleteChatId);
      delete state.messagesByChatId[deleteChatId];
      return { success: true, deleted: true, chatId: deleteChatId } as T;
    }
    state.chats = state.chats.filter((chat) => chat.id !== deleteChatId);
    delete state.messagesByChatId[deleteChatId];
    return { success: true, message: 'deleted in browser mock', chatId: deleteChatId } as T;
  }

  throw new Error(`Browser dev mock does not implement ${method} ${path}`);
}

function pushSse(
  onEvent: (payload: LocalBackendStreamEvent) => void,
  data: unknown,
  event?: string,
) {
  const prefix = event ? `event: ${event}\n` : '';
  const payload = typeof data === 'string' ? data : JSON.stringify(data);
  onEvent({ type: 'data', chunk: `${prefix}data: ${payload}\n\n` });
}

function scheduleStreamTimer(streamId: string, callback: () => void, delay: number) {
  const timer = window.setTimeout(callback, delay);
  const timers = state.activeStreams.get(streamId) ?? [];
  timers.push(timer);
  state.activeStreams.set(streamId, timers);
}

async function startStream(
  input: LocalBackendRequestInput,
  onEvent: (payload: LocalBackendStreamEvent) => void,
): Promise<string | null> {
  const chatId = getChatIdFromPath(input.path, '/run');
  if (!chatId) {
    onEvent({ type: 'error', error: `Invalid mock stream path: ${input.path}` });
    return null;
  }

  const body = input.body as { message?: string } | undefined;
  const message = body?.message?.trim() || '请演示一下浏览器 mock 回复。';
  const streamId = `browser-stream-${Date.now()}`;
  state.activeStreams.set(streamId, []);

  const userMessage = createMessage({
    id: `msg-user-${Date.now()}`,
    chatId,
    role: 'user',
    content: message,
    minutesAgo: 0,
  });
  userMessage.createdAt = new Date().toISOString();
  state.messagesByChatId[chatId] = [...(state.messagesByChatId[chatId] ?? []), userMessage];

  const reply =
    `这是浏览器 dev mock 的流式回复。你刚才输入的是：「${message}」。` +
    ' 这里可以调试消息气泡、滚动、停止按钮、工具执行卡片和后续状态。';
  const chunks = reply.match(/.{1,8}/g) ?? [reply];
  const reasoning = '先调用一次 mock 工具，再把回复按调用流程交错显示。';
  const mockAction = {
    id: 'browser-mock-1',
    tool: 'browser_mock',
    arguments: { path: input.path },
    result: { success: true, output: 'Generated mock stream in browser preview.' },
  };

  scheduleStreamTimer(streamId, () => {
    pushSse(onEvent, { type: 'reasoning', content: reasoning });
  }, 80);

  scheduleStreamTimer(streamId, () => {
    pushSse(onEvent, {
      type: 'executed_actions',
      actions: [{ ...mockAction, result: undefined }],
    });
  }, 160);

  scheduleStreamTimer(streamId, () => {
    pushSse(onEvent, {
      type: 'executed_actions',
      actions: [mockAction],
    });
  }, 280);

  chunks.forEach((chunk, index) => {
    scheduleStreamTimer(streamId, () => {
      pushSse(onEvent, { content: chunk });
    }, 360 + index * 80);
  });

  scheduleStreamTimer(streamId, () => {
    const assistantMessage = createMessage({
      id: `msg-assistant-${Date.now()}`,
      chatId,
      role: 'assistant',
      content: reply,
      minutesAgo: 0,
      messageMetadata: JSON.stringify({
        executedActions: [mockAction],
        timeline: [
          { type: 'reasoning', content: reasoning },
          { type: 'tools', actions: [mockAction] },
          { type: 'text', content: reply },
        ],
        durationMs: 1_200,
        suggestedReplies: ['继续完善这份结果', '换一种呈现方式', '告诉我下一步怎么做'],
      }),
    });
    assistantMessage.createdAt = new Date().toISOString();
    state.messagesByChatId[chatId] = [
      ...(state.messagesByChatId[chatId] ?? []),
      assistantMessage,
    ];
    updateChatTimestamp(chatId);
    pushSse(onEvent, { type: 'message_id', messageId: assistantMessage.id });
    pushSse(onEvent, '[DONE]');
    onEvent({ type: 'end', status: 200 });
    state.activeStreams.delete(streamId);
    const suggestions = ['继续完善这份结果', '换一种呈现方式', '告诉我下一步怎么做'];
    for (const listener of suggestedReplyListeners) {
      listener({ chatId, messageId: assistantMessage.id, suggestions });
    }
  }, 440 + chunks.length * 80);

  return streamId;
}

function cancelStream(streamId: string) {
  const timers = state.activeStreams.get(streamId) ?? [];
  timers.forEach((timer) => window.clearTimeout(timer));
  state.activeStreams.delete(streamId);
}

export function installBrowserDevElectronMock() {
  if (typeof window === 'undefined' || window.electron) return;
  // DEV 浏览器预览自动安装；官网静态 demo 构建（app-demo 入口）经 demo flag
  // 显式安装。普通 prod 构建两条路都不通，本模块根本不会进产物。
  if (!import.meta.env.DEV && !isDemoMode()) return;

  window.electron = {
    runtime: 'local',
    platform: 'win32',
    local: {
      selectDirectory: async () => ({
        canceled: false,
        filePaths: ['C:\\browser-preview\\mock-skill'],
      }),
      // 浏览器 dev 模式没有 Electron capturePage/clipboard，如实返回失败。
      captureScreenshot: async () => ({
        success: false as const,
        error: '浏览器开发模式不支持窗口截图（需要 Electron 运行时）',
      }),
    },
    localBackend: {
      request,
      startStream,
      cancelStream,
    },
    onMenuNewChat: () => undefined,
    offMenuNewChat: () => undefined,
    onMenuOpenTerminal: () => undefined,
    offMenuOpenTerminal: () => undefined,
    onChatTitleUpdated: () => () => undefined,
    onSuggestedReplies: (callback) => {
      suggestedReplyListeners.add(callback);
      return () => {
        suggestedReplyListeners.delete(callback);
      };
    },
  } satisfies ElectronBridge;

  console.info(
    '[browser-dev-electron-mock] Installed mock window.electron bridge.',
    state.source === 'fixture'
      ? { source: state.fixtureMeta?.source, exportedAt: state.fixtureMeta?.exportedAt }
      : { source: 'built-in fallback' },
  );
}
