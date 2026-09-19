/**
 * LocalBackendRouter 测试脚手架。
 *
 * router.ts 的模块级依赖在这里全部替掉：
 *   - storage/index.js   —— 真实 localStore 会加载 Electron ABI 的
 *     better-sqlite3，plain vitest 下不可用；换成语义镜像的内存 FakeLocalStore。
 *   - llm/index.js       —— llmService 设置 + sidecar 句柄（按用例挂/摘）。
 *   - coreloop-stream.js —— streamCoreLoopTurn 是 sidecar 流边界，换成
 *     可按脚本回放 onText / onTool 事件 / 终态的桩；getActiveCoreLoopStreamId 走内存表。
 *   - skill-loader / ai-title / insights / egress / pack-* / image-attachment /
 *     project-rules / llm-diagnose / skill-install / local-executor —— 全部
 *     换成可控 spy，避免碰文件系统/网络/子进程。
 *
 * 纯函数模块（agent-capability、message-triggers、regenerate-helper、
 * branch-helper、interrupted-helper、history-helper、turn-timeline、
 * turn-duration、context-compactor、auto-continue-helper、prompt-builder、
 * exec-sandbox、live-stream）保留真实实现——router 的行为断言尽量落在
 * 真实拼装逻辑上，mock 只停在进程外边界。
 */
import { vi } from 'vitest';

import type {
  ChatAgentRecord,
  ChatMessageRecord,
  ChatSessionRecord,
  HarnessTraceRecord,
  TaskRecord,
} from '../../src/storage/index.js';
import type { LocalBackendBroadcast } from '../../src/local-backend/router.js';

// ---------------------------------------------------------------------------
// 内存 FakeLocalStore：语义镜像 src/storage/index.ts 的 SQL 实现
// （listMessages DESC、deleteMessagesFrom 截断、updateChat undefined 跳过）。
// ---------------------------------------------------------------------------

interface FakeStoreState {
  chats: Map<string, ChatSessionRecord>;
  messages: ChatMessageRecord[];
  agents: Map<string, ChatAgentRecord>;
  recordIds: Map<string, string>;
  turnActive: Map<string, { startedAt: string }>;
  traces: Map<string, HarnessTraceRecord>;
  tasks: TaskRecord[];
  usageEvents: Array<Record<string, unknown>>;
  telemetry: { endpoint: string | null; privacyMode: string; serviceName: string | null } | null;
  webSearch: { provider: string; apiKey: string | null } | null;
  insights: {
    shareBehavior: boolean;
    shareConversation: boolean;
    shareProfile: boolean;
    promptedAt: string | null;
    apiBase: string | null;
    profile: { displayName: string; email: string; company: string; note: string };
  };
  llmSettings: Record<string, unknown> | null;
}

function initialStoreState(): FakeStoreState {
  return {
    chats: new Map(),
    messages: [],
    agents: new Map(),
    recordIds: new Map(),
    turnActive: new Map(),
    traces: new Map(),
    tasks: [],
    usageEvents: [],
    telemetry: null,
    webSearch: null,
    insights: {
      shareBehavior: false,
      shareConversation: false,
      shareProfile: false,
      promptedAt: null,
      apiBase: null,
      profile: { displayName: '', email: '', company: '', note: '' },
    },
    llmSettings: null,
  };
}

class FakeLocalStore {
  state = initialStoreState();
  private seq = 0;

  async reset(): Promise<void> {
    this.state = initialStoreState();
    this.seq = 0;
  }

  private now(): string {
    return new Date().toISOString();
  }

  // ── chats ──
  async listChats(page = 1, limit = 50): Promise<{ chats: ChatSessionRecord[]; total: number }> {
    // SQL 实现：ORDER BY is_pinned DESC, datetime(updated_at) DESC。
    const all = [...this.state.chats.values()].sort(
      (a, b) =>
        Number(b.isPinned) - Number(a.isPinned) || b.updatedAt.localeCompare(a.updatedAt),
    );
    const start = (page - 1) * limit;
    return { chats: all.slice(start, start + limit), total: all.length };
  }

  async getChat(chatId: string): Promise<ChatSessionRecord | null> {
    return this.state.chats.get(chatId) ?? null;
  }

  async createChat(title: string, agentId: string, projectId: string | null): Promise<ChatSessionRecord> {
    return await this.createChatWithId(`chat-${++this.seq}`, title, agentId, projectId);
  }

  async createChatWithId(
    id: string,
    title: string,
    agentId: string,
    projectId: string | null = null,
  ): Promise<ChatSessionRecord> {
    const now = this.now();
    const chat: ChatSessionRecord = {
      id,
      title,
      userId: 'local',
      agentId,
      projectId,
      createdAt: now,
      updatedAt: now,
      isPinned: false,
      systemPrompt: null,
      pinnedRefs: null,
    };
    this.state.chats.set(id, chat);
    return chat;
  }

  async updateChat(
    chatId: string,
    updates: Partial<{
      title: string;
      isPinned: boolean;
      systemPrompt: string | null;
      pinnedRefs: unknown[] | null;
      projectId: string | null;
    }>,
  ): Promise<ChatSessionRecord | null> {
    const chat = this.state.chats.get(chatId);
    if (!chat) return null;
    const next = { ...chat };
    for (const [key, value] of Object.entries(updates)) {
      if (value !== undefined) (next as Record<string, unknown>)[key] = value;
    }
    next.updatedAt = this.now();
    this.state.chats.set(chatId, next);
    return next;
  }

  async deleteChat(chatId: string): Promise<boolean> {
    const existed = this.state.chats.delete(chatId);
    this.state.messages = this.state.messages.filter((m) => m.chatId !== chatId);
    return existed;
  }

  async deleteChatIfEmpty(chatId: string): Promise<boolean> {
    const chat = this.state.chats.get(chatId);
    if (!chat) return false;
    if (this.state.messages.some((m) => m.chatId === chatId)) return false;
    return await this.deleteChat(chatId);
  }

  async deleteEmptyChats(exceptChatId?: string | null): Promise<string[]> {
    const deleted: string[] = [];
    for (const chat of [...this.state.chats.values()]) {
      if (exceptChatId && chat.id === exceptChatId) continue;
      if (this.state.messages.some((m) => m.chatId === chat.id)) continue;
      this.state.chats.delete(chat.id);
      deleted.push(chat.id);
    }
    return deleted;
  }

  async clearProjectAssignment(projectId: string): Promise<number> {
    let count = 0;
    for (const chat of this.state.chats.values()) {
      if (chat.projectId === projectId) {
        chat.projectId = null;
        count += 1;
      }
    }
    return count;
  }

  async getChatRecordId(chatId: string): Promise<string | null> {
    return this.state.recordIds.get(chatId) ?? null;
  }

  async setChatRecordId(chatId: string, recordId: string): Promise<void> {
    this.state.recordIds.set(chatId, recordId);
  }

  // ── turn_active 标记（W7-1 中断签名）──
  async setTurnActive(chatId: string): Promise<void> {
    this.state.turnActive.set(chatId, { startedAt: this.now() });
  }

  async clearTurnActive(chatId: string): Promise<void> {
    this.state.turnActive.delete(chatId);
  }

  async getTurnActive(chatId: string): Promise<{ startedAt: string } | null> {
    return this.state.turnActive.get(chatId) ?? null;
  }

  // ── messages（listMessages 返回 DESC，与 SQL ORDER BY created_at DESC 对齐）──
  async listMessages(chatId: string, limit = 200): Promise<ChatMessageRecord[]> {
    return this.state.messages
      .filter((m) => m.chatId === chatId)
      .slice()
      .sort((a, b) => b.id.localeCompare(a.id))
      .slice(0, limit);
  }

  async addMessage(
    chatId: string,
    role: ChatMessageRecord['role'],
    content: string,
    messageMetadata: string | null = null,
  ): Promise<ChatMessageRecord> {
    const record: ChatMessageRecord = {
      // seq 前缀保证字典序即插入序（零填充），listMessages 的 DESC 排序稳定。
      id: `msg-${String(++this.seq).padStart(6, '0')}`,
      chatId,
      role,
      content,
      messageMetadata,
      createdAt: this.now(),
    };
    this.state.messages.push(record);
    return record;
  }

  async getMessage(chatId: string, messageId: string): Promise<ChatMessageRecord | null> {
    return this.state.messages.find((m) => m.chatId === chatId && m.id === messageId) ?? null;
  }

  async patchMessageMetadata(
    chatId: string,
    messageId: string,
    patch: Record<string, unknown>,
  ): Promise<ChatMessageRecord | null> {
    const msg = await this.getMessage(chatId, messageId);
    if (!msg) return null;
    let current: Record<string, unknown> = {};
    if (msg.messageMetadata) {
      try {
        const parsed = JSON.parse(msg.messageMetadata) as unknown;
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          current = parsed as Record<string, unknown>;
        }
      } catch {
        current = {};
      }
    }
    msg.messageMetadata = JSON.stringify({ ...current, ...patch });
    return msg;
  }

  async deleteMessagesFrom(chatId: string, messageId: string): Promise<number> {
    const target = this.state.messages.find((m) => m.chatId === chatId && m.id === messageId);
    if (!target) return 0;
    const keep = this.state.messages.filter(
      (m) => !(m.chatId === chatId && m.id >= target.id),
    );
    const deleted = this.state.messages.length - keep.length;
    this.state.messages = keep;
    return deleted;
  }

  async replaceChatMessages(
    chatId: string,
    messages: Array<{ role: ChatMessageRecord['role']; content: string }>,
  ): Promise<void> {
    this.state.messages = this.state.messages.filter((m) => m.chatId !== chatId);
    for (const m of messages) await this.addMessage(chatId, m.role, m.content);
  }

  // ── chat agents ──
  async listChatAgents(includeArchived = false): Promise<ChatAgentRecord[]> {
    return [...this.state.agents.values()].filter((a) => includeArchived || !a.isArchived);
  }

  async getChatAgent(agentId: string): Promise<ChatAgentRecord | null> {
    return this.state.agents.get(agentId) ?? null;
  }

  async createChatAgent(
    input: Partial<ChatAgentRecord> & { name: string },
  ): Promise<ChatAgentRecord> {
    const now = this.now();
    const agent: ChatAgentRecord = {
      id: `agent-${++this.seq}`,
      slug: null,
      name: input.name,
      icon: input.icon ?? null,
      color: input.color ?? null,
      description: input.description ?? null,
      rolePrompt: input.rolePrompt ?? null,
      forbiddenPrompt: input.forbiddenPrompt ?? null,
      skillIds: input.skillIds ?? [],
      toolPolicy: input.toolPolicy ?? { mode: 'all', tools: [] },
      allowExternalSkills: input.allowExternalSkills ?? true,
      loadAllSkills: input.loadAllSkills ?? false,
      isBuiltin: input.isBuiltin ?? false,
      isArchived: input.isArchived ?? false,
      sortOrder: input.sortOrder ?? 0,
      createdAt: now,
      updatedAt: now,
    };
    this.state.agents.set(agent.id, agent);
    return agent;
  }

  async updateChatAgent(
    agentId: string,
    updates: Partial<ChatAgentRecord>,
  ): Promise<ChatAgentRecord | null> {
    const agent = this.state.agents.get(agentId);
    if (!agent) return null;
    const next = { ...agent };
    for (const [key, value] of Object.entries(updates)) {
      if (value !== undefined) (next as Record<string, unknown>)[key] = value;
    }
    next.updatedAt = this.now();
    this.state.agents.set(agentId, next);
    return next;
  }

  async archiveChatAgent(agentId: string): Promise<boolean> {
    const agent = this.state.agents.get(agentId);
    if (!agent) return false;
    agent.isArchived = true;
    return true;
  }

  // ── tasks / traces / usage ──
  async listTasks(chatId?: string): Promise<TaskRecord[]> {
    return this.state.tasks.filter((t) => !chatId || t.chatId === chatId);
  }

  async saveTrace(input: {
    id: string;
    chatId: string;
    messageId: string | null;
    startedAtMs: number;
    durationMs: number | null;
    status: string;
    payload: unknown;
  }): Promise<void> {
    this.state.traces.set(input.id, {
      ...input,
      // SQL 实现里 payload 是 JSON 文本列——路由读取时经 safeJson 解析。
      payload: JSON.stringify(input.payload),
      createdAt: this.now(),
    } as HarnessTraceRecord);
  }

  async listTracesByChat(chatId: string, limit = 50): Promise<HarnessTraceRecord[]> {
    return [...this.state.traces.values()].filter((t) => t.chatId === chatId).slice(0, limit);
  }

  async getTrace(traceId: string): Promise<HarnessTraceRecord | null> {
    return this.state.traces.get(traceId) ?? null;
  }

  async recordUsageEvent(input: Record<string, unknown>): Promise<void> {
    this.state.usageEvents.push(input);
  }

  async getUsageSummary(sinceDays = 30): Promise<Record<string, unknown>> {
    return { days: sinceDays, events: this.state.usageEvents.length };
  }

  // ── settings ──
  async getLlmSettings(): Promise<Record<string, unknown> | null> {
    return this.state.llmSettings;
  }

  async getTelemetrySettings(): Promise<FakeStoreState['telemetry']> {
    return this.state.telemetry;
  }

  async setTelemetrySettings(patch: Record<string, unknown>): Promise<NonNullable<FakeStoreState['telemetry']>> {
    const next = {
      endpoint: null as string | null,
      privacyMode: 'metadata',
      serviceName: null as string | null,
      ...this.state.telemetry,
    };
    for (const [key, value] of Object.entries(patch)) {
      if (value !== undefined) (next as Record<string, unknown>)[key] = value;
    }
    this.state.telemetry = next;
    return next;
  }

  async getWebSearchSettings(): Promise<FakeStoreState['webSearch']> {
    return this.state.webSearch;
  }

  async setWebSearchSettings(patch: Record<string, unknown>): Promise<NonNullable<FakeStoreState['webSearch']>> {
    const next = { provider: 'tavily', apiKey: null as string | null, ...this.state.webSearch };
    for (const [key, value] of Object.entries(patch)) {
      if (value !== undefined) (next as Record<string, unknown>)[key] = value;
    }
    this.state.webSearch = next;
    return next;
  }

  async ensureInsightsSettings(): Promise<FakeStoreState['insights']> {
    return this.state.insights;
  }

  async setInsightsSettings(patch: {
    shareBehavior?: boolean;
    shareConversation?: boolean;
    shareProfile?: boolean;
    promptedAt?: string;
    apiBase?: string;
    profile?: Partial<FakeStoreState['insights']['profile']>;
  }): Promise<FakeStoreState['insights']> {
    const current = this.state.insights;
    this.state.insights = {
      ...current,
      ...(patch.shareBehavior !== undefined ? { shareBehavior: patch.shareBehavior } : {}),
      ...(patch.shareConversation !== undefined
        ? { shareConversation: patch.shareConversation }
        : {}),
      ...(patch.shareProfile !== undefined ? { shareProfile: patch.shareProfile } : {}),
      ...(patch.promptedAt !== undefined ? { promptedAt: patch.promptedAt } : {}),
      ...(patch.apiBase !== undefined ? { apiBase: patch.apiBase } : {}),
      profile: { ...current.profile, ...patch.profile },
    };
    return this.state.insights;
  }

  async insightStats(): Promise<{ events: number; turns: number; profile: number; pending: number }> {
    return { events: 0, turns: 0, profile: 0, pending: 0 };
  }
}

// ---------------------------------------------------------------------------
// hoisted 共享状态：vi.mock 工厂与用例之间的控制面。
// ---------------------------------------------------------------------------

// vitest 不允许直接 export vi.hoisted 的结果；内部用 harness 持有，
// 再经普通导出暴露给用例（vi.mock 工厂闭包引用的是 hoisted 绑定）。
const harness = vi.hoisted(() => {
  return {
    // 类声明存在 TDZ，不能在这里 new；store 在下方模块求值时赋值，
    // vi.mock 工厂在测试文件 import router 时才执行，那时已就绪。
    store: null as unknown as FakeLocalStore,
    /** 技能删除路由的 userSkillsDir（真实 fs 操作，用例可覆写到临时目录）。 */
    userSkillsDir: '/tmp/router-test-user-skills',
    /** getSidecarSupervisor() 的返回值（同步路径：流式/cancel/branches）。 */
    supervisor: null as unknown,
    /** whenSidecarSupervisor() 的返回值（设置页只读路径）。 */
    pendingSupervisor: null as unknown,
    llmSettings: {
      provider: 'openai-compat',
      model: 'unit-test-model',
      baseUrl: 'http://127.0.0.1:9/v1',
      apiKey: 'unit-test-key',
      temperature: undefined as number | undefined,
      systemPrompt: undefined as string | undefined,
      maxTotalTokens: undefined as number | undefined,
      execTimeoutSeconds: undefined as number | undefined,
      compat: undefined as unknown,
      presets: undefined as unknown,
      vendorId: undefined as string | undefined,
    },
    setSettings: vi.fn(),
    setDefaultExecTimeoutMs: vi.fn(),
    allowEgressForBaseUrl: vi.fn(async () => true),
    egressBroker: null as unknown,
    egressPosture: null as unknown,
    /** chatId → streamId；coreloop-stream 活跃流注册表的替身。 */
    activeStreamIds: new Map<string, string>(),
    /** streamCoreLoopTurn 的每用例实现；(options) => Promise<outcome>。 */
    streamImpl: null as
      | null
      | ((options: Record<string, unknown>) => Promise<Record<string, unknown>>),
    loadSkills: vi.fn(),
    findSkill: vi.fn(),
    installSkillFromDirectory: vi.fn(() => ({ name: 'imported-skill', dest: '/tmp/dest' })),
    generateChatTitle: vi.fn(async () => ({ title: '生成的标题', usedFallback: false })),
    generateSuggestedReplies: vi.fn(async () => ({
      suggestions: ['llm-追问-1', 'llm-追问-2', 'llm-追问-3'],
      usedFallback: false,
    })),
    diagnoseLlmConnection: vi.fn(async () => ({ ok: true, steps: [] })),
    parseImageAttachments: vi.fn((value: unknown) => (Array.isArray(value) ? value : [])),
    processImageAttachments: vi.fn(() => ({ images: [], notes: [] as string[] })),
    loadProjectRuleFiles: vi.fn(() => ({ files: [] as string[], content: '' })),
    /** matchPackBackendRoute 的每用例返回值。 */
    packRoute: null as null | {
      params: Record<string, string>;
      route: { handler: (req: unknown) => unknown };
    },
    recordInsightEvent: vi.fn(),
    recordInsightTurn: vi.fn(),
    recordInsightProfile: vi.fn(),
    buildInsightsExportPayload: vi.fn(() => ({ exported: true })),
    flushInsightsOutbox: vi.fn(async () => ({ sent: 0 })),
    uploadInsightsBundle: vi.fn(async () => true),
    /** open-path 路由的宿主打开能力（'' = 成功，非空 = 错误消息）。 */
    shellOpenPath: vi.fn(async (_target: string) => ''),
  };
});

export const h = harness;

vi.mock('../../src/storage/index.js', () => ({
  localStore: h.store,
  DEFAULT_SYSTEM_PROMPT: 'DEFAULT_SYSTEM_PROMPT',
  telemetryEnabled: (s: { endpoint?: string | null } | null | undefined) =>
    Boolean(s?.endpoint),
}));

vi.mock('../../src/llm/index.js', () => ({
  llmService: {
    getSettings: () => h.llmSettings,
    setSettings: h.setSettings,
  },
  getSidecarSupervisor: () => h.supervisor,
  whenSidecarSupervisor: async () => h.pendingSupervisor,
}));

vi.mock('../../src/runtime.js', () => ({
  getAppRootDir: () => '/tmp/app-root',
  shellOpenPath: (target: string) => h.shellOpenPath(target),
}));

// ToolRouter 仅以类型出现在 router.ts；提供空类避免加载真实模块（它会拖入
// local-executor / node-pty 等重依赖）。用例一律持有 stub 实例。
vi.mock('../../src/tool-router.js', () => ({ ToolRouter: class ToolRouter {} }));

vi.mock('../../src/local-executor.js', () => ({
  setDefaultExecTimeoutMs: h.setDefaultExecTimeoutMs,
}));

vi.mock('../../src/sidecar/egress-proxy.js', () => ({
  allowEgressForBaseUrl: h.allowEgressForBaseUrl,
  getActiveEgressBroker: () => h.egressBroker,
  getEgressPosture: () => h.egressPosture,
  getActiveEgressProxyEndpoint: () => null,
}));

vi.mock('../../src/sidecar/index.js', () => ({
  SidecarSupervisor: class SidecarSupervisor {
    static lastSpawnRefusal: unknown = null;
  },
}));

vi.mock('../../src/local-backend/coreloop-stream.js', () => ({
  buildWorldState: (input: unknown) => ({ mockedWorldState: true, input }),
  getActiveCoreLoopStreamId: (chatId: string) => h.activeStreamIds.get(chatId),
  streamCoreLoopTurn: (options: Record<string, unknown>) => {
    if (!h.streamImpl) throw new Error('streamImpl not configured');
    return h.streamImpl(options);
  },
}));

vi.mock('../../src/local-backend/skill-loader.js', () => ({
  loadSkills: h.loadSkills,
  findSkill: h.findSkill,
  getUserSkillsDir: () => h.userSkillsDir,
  getSkillsDir: () => '/tmp/builtin-skills',
  listSkillRoots: () => [],
  classifySkillOrigin: (dir: string) =>
    dir.includes('user') ? 'user' : dir.includes('workspace') ? 'workspace' : 'builtin',
  setPackSkillsDir: () => {},
  setWorkspaceSkillRootsProvider: () => {},
  bindWorkspaceSkillRoots: () => {},
}));

vi.mock('../../src/local-backend/skill-install.js', () => ({
  installSkillFromDirectory: h.installSkillFromDirectory,
}));

vi.mock('../../src/local-backend/ai-title.js', () => ({
  generateChatTitle: h.generateChatTitle,
}));

vi.mock('../../src/local-backend/ai-suggestions.js', () => ({
  generateSuggestedReplies: h.generateSuggestedReplies,
}));

vi.mock('../../src/local-backend/llm-diagnose.js', () => ({
  diagnoseLlmConnection: h.diagnoseLlmConnection,
}));

vi.mock('../../src/image-attachment.js', () => ({
  parseImageAttachments: h.parseImageAttachments,
  processImageAttachments: h.processImageAttachments,
}));

vi.mock('../../src/project-rules.js', () => ({
  loadProjectRuleFiles: h.loadProjectRuleFiles,
}));

vi.mock('../../src/local-backend/pack-turn-hooks.js', () => ({
  beginPackTurnObservers: () => [],
  collectPackExecWritableRoots: () => [],
  collectPackForcedSkillVars: () => ({}),
  collectPackWorldState: () => ({}),
}));

vi.mock('../../src/local-backend/pack-backend-routes.js', () => ({
  matchPackBackendRoute: () => h.packRoute,
}));

vi.mock('../../src/insights/record.js', () => ({
  recordInsightEvent: h.recordInsightEvent,
  recordInsightTurn: h.recordInsightTurn,
  recordInsightProfile: h.recordInsightProfile,
}));

vi.mock('../../src/insights/flush.js', () => ({
  buildInsightsExportPayload: h.buildInsightsExportPayload,
  flushInsightsOutbox: h.flushInsightsOutbox,
  uploadInsightsBundle: h.uploadInsightsBundle,
}));

// 模块求值时建好内存存储（见 h.store 上的注释）。
h.store = new FakeLocalStore();

// ---------------------------------------------------------------------------
// 每用例复位：清存储、清 sidecar 句柄、恢复 spy 的默认实现。
// ---------------------------------------------------------------------------

export function resetRouterTestkit(): void {
  h.store.reset();
  h.supervisor = null;
  h.pendingSupervisor = null;
  h.egressBroker = null;
  h.egressPosture = null;
  h.activeStreamIds.clear();
  h.streamImpl = null;
  h.packRoute = null;
  h.userSkillsDir = '/tmp/router-test-user-skills';
  h.llmSettings = {
    provider: 'openai-compat',
    model: 'unit-test-model',
    baseUrl: 'http://127.0.0.1:9/v1',
    apiKey: 'unit-test-key',
    temperature: undefined,
    systemPrompt: undefined,
    maxTotalTokens: undefined,
    execTimeoutSeconds: undefined,
    compat: undefined,
    presets: undefined,
    vendorId: undefined,
  };
  h.setSettings.mockReset();
  h.setSettings.mockImplementation((patch: Record<string, unknown>) => ({
    ...h.llmSettings,
    ...patch,
  }));
  h.setDefaultExecTimeoutMs.mockReset();
  h.allowEgressForBaseUrl.mockReset();
  h.allowEgressForBaseUrl.mockResolvedValue(true);
  h.loadSkills.mockReset();
  h.loadSkills.mockResolvedValue([]);
  h.findSkill.mockReset();
  h.findSkill.mockResolvedValue(null);
  h.installSkillFromDirectory.mockReset();
  h.installSkillFromDirectory.mockReturnValue({ name: 'imported-skill', dest: '/tmp/dest' });
  h.generateChatTitle.mockReset();
  h.generateChatTitle.mockResolvedValue({ title: '生成的标题', usedFallback: false });
  h.generateSuggestedReplies.mockReset();
  h.generateSuggestedReplies.mockResolvedValue({
    suggestions: ['llm-追问-1', 'llm-追问-2', 'llm-追问-3'],
    usedFallback: false,
  });
  h.diagnoseLlmConnection.mockReset();
  h.diagnoseLlmConnection.mockResolvedValue({ ok: true, steps: [] });
  h.parseImageAttachments.mockReset();
  h.parseImageAttachments.mockImplementation((value: unknown) =>
    Array.isArray(value) ? value : [],
  );
  h.processImageAttachments.mockReset();
  h.processImageAttachments.mockReturnValue({ images: [], notes: [] });
  h.loadProjectRuleFiles.mockReset();
  h.loadProjectRuleFiles.mockReturnValue({ files: [], content: '' });
  h.recordInsightEvent.mockReset();
  h.recordInsightTurn.mockReset();
  h.recordInsightProfile.mockReset();
  h.buildInsightsExportPayload.mockReset();
  h.buildInsightsExportPayload.mockReturnValue({ exported: true });
  h.flushInsightsOutbox.mockReset();
  h.flushInsightsOutbox.mockResolvedValue({ sent: 0 });
  h.uploadInsightsBundle.mockReset();
  h.uploadInsightsBundle.mockResolvedValue(true);
  h.shellOpenPath.mockReset();
  h.shellOpenPath.mockResolvedValue('');
}

// ---------------------------------------------------------------------------
// 依赖 stub 工厂
// ---------------------------------------------------------------------------

export interface ToolSchemaStub {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  mode: 'read' | 'write';
}

export const DEFAULT_TOOL_SCHEMAS: ToolSchemaStub[] = [
  { name: 'local_read_file', description: '读文件', inputSchema: { type: 'object' }, mode: 'read' },
  { name: 'local_exec_shell', description: '执行命令', inputSchema: { type: 'object' }, mode: 'read' },
  { name: 'local_write_file', description: '写文件', inputSchema: { type: 'object' }, mode: 'write' },
];

/**
 * ToolRouter stub。listModelSchemas 按真实语义应用工具策略
 * （allowlist/denylist/all），让路由测试能断言策略真实收窄了工具面。
 */
export function makeToolRouter(overrides: Record<string, unknown> = {}) {
  const schemas = (overrides.schemas as ToolSchemaStub[] | undefined) ?? DEFAULT_TOOL_SCHEMAS;
  const applyPolicy = (policy?: { mode: string; tools: string[] }) => {
    if (!policy || policy.mode === 'all') return schemas;
    if (policy.mode === 'allowlist') return schemas.filter((s) => policy.tools.includes(s.name));
    if (policy.mode === 'denylist') return schemas.filter((s) => !policy.tools.includes(s.name));
    return schemas;
  };
  return {
    execute: vi.fn(async () => ({ content: 'tool-output' })),
    listSchemas: vi.fn(() => schemas),
    listModelSchemas: vi.fn((policy?: { mode: string; tools: string[] }) => applyPolicy(policy)),
    getSchemaByName: vi.fn((name: string) => schemas.find((s) => s.name === name) ?? null),
    projectRegistry: null as unknown,
    mcpRegistry: null as unknown,
    ...overrides,
  };
}

export interface FakeProject {
  id: string;
  name: string;
  folderPath: string;
  trusted: boolean;
}

/** 项目注册表内存实现：create/update/delete/setTrusted 语义镜像 ProjectRegistry。 */
export function makeProjectRegistry(initial: FakeProject[] = []) {
  const projects = new Map(initial.map((p) => [p.id, { ...p }]));
  let seq = projects.size;
  return {
    projects,
    get: vi.fn((id: string) => projects.get(id) ?? null),
    list: vi.fn(() => [...projects.values()]),
    create: vi.fn((input: { name: string; folderPath: string }) => {
      if (!input.name.trim()) throw new Error('项目名称不能为空');
      if (!input.folderPath.trim()) throw new Error('项目路径不能为空');
      const project: FakeProject = {
        id: `proj-${++seq}`,
        name: input.name,
        folderPath: input.folderPath,
        trusted: false,
      };
      projects.set(project.id, project);
      return project;
    }),
    update: vi.fn((id: string, patch: { name?: string; folderPath?: string }) => {
      const project = projects.get(id);
      if (!project) throw new Error('项目不存在');
      if (patch.name !== undefined) project.name = patch.name;
      if (patch.folderPath !== undefined) project.folderPath = patch.folderPath;
      return project;
    }),
    delete: vi.fn((id: string) => projects.delete(id)),
    setTrusted: vi.fn((id: string, trusted: boolean) => {
      const project = projects.get(id);
      if (!project) throw new Error('项目不存在');
      project.trusted = trusted;
      return project;
    }),
    isTrusted: vi.fn((id: string) => projects.get(id)?.trusted === true),
  };
}

/** MCP 注册表 stub：servers 为原始记录，toolCache 控制 getCachedTools 命中。 */
export function makeMcpRegistry(options: {
  servers?: Array<Record<string, unknown>>;
  toolCache?: Map<string, { tools: Array<{ name: string; description?: string }>; error: string | null; fetchedAt: string }>;
  toolEntries?: Array<Record<string, unknown>>;
  toolsByToken?: Map<string, Record<string, unknown>>;
} = {}) {
  const servers = options.servers ?? [];
  const toolCache = options.toolCache ?? new Map();
  return {
    list: vi.fn(() => servers),
    getCachedTools: vi.fn((id: string) => toolCache.get(id) ?? null),
    serverKey: vi.fn((s: { id: string }) => `key-${s.id}`),
    refreshTools: vi.fn(async (id: string) => {
      const cached = toolCache.get(id);
      return cached ?? { tools: [], error: null, fetchedAt: new Date().toISOString() };
    }),
    listEnabledToolEntries: vi.fn(() => options.toolEntries ?? []),
    findToolByToken: vi.fn((token: string) => options.toolsByToken?.get(token) ?? null),
    create: vi.fn((input: Record<string, unknown>) => ({
      id: 'srv-1',
      enabled: true,
      ...input,
    })),
    update: vi.fn((id: string, patch: Record<string, unknown>) => {
      const server = servers.find((s) => s.id === id);
      if (!server) throw new Error('server not found');
      // undefined 表示「未提供该字段」，不覆盖原值（路由会把整个归一后的
      // patch 传进来，未提供的键是 undefined）。
      const clean = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined));
      return { ...server, ...clean };
    }),
    delete: vi.fn((id: string) => servers.some((s) => s.id === id)),
    importClaudeConfig: vi.fn(() => ({ added: [], skipped: [] })),
  };
}

/** sidecar supervisor stub：流式回合之外的 RPC 面（branches/tree/messages/fork/trace/models）。 */
export function makeSupervisor(overrides: Record<string, unknown> = {}) {
  return {
    call: vi.fn(async (method: string) => {
      if (method === 'trace.fetch') {
        return { trace: { durationMs: 42 }, spans: [{ name: 'span' }], events: [] };
      }
      return {};
    }),
    cancelChat: vi.fn(async () => {}),
    sessionBranches: vi.fn(async () => ({
      lineage: [{ recordId: 'rec-1' }],
      children: [{ recordId: 'rec-2' }],
    })),
    sessionTree: vi.fn(async () => ({
      tree: { recordId: 'rec-1', children: [{ recordId: 'rec-2', children: [] }] },
      nodeCount: 2,
      truncated: false,
    })),
    sessionMessages: vi.fn(async () => ({
      messages: [
        { role: 'user', content: '分支里的用户消息' },
        { role: 'assistant', content: '分支里的助手回复' },
        { role: 'tool', content: '{"toolCallId":"t1"}' },
      ],
    })),
    forkSession: vi.fn(async () => ({
      ok: true,
      fork: { recordId: 'fork-rec-1', label: 'fork' },
    })),
    listModels: vi.fn(async () => ({ models: [{ id: 'm1' }], catalogStatus: 'ok' })),
    getSandboxPosture: vi.fn(() => ({ sandboxed: true, backend: 'seatbelt' })),
    ...overrides,
  };
}

/** 广播捕获：main.ts 注入的 webContents.send 替身。 */
export function makeBroadcast() {
  const calls: Array<{ event: string; payload: unknown }> = [];
  const broadcast: LocalBackendBroadcast = (event, payload) => {
    calls.push({ event, payload });
  };
  return { broadcast, calls };
}

// ---------------------------------------------------------------------------
// SSE 捕获与解析
// ---------------------------------------------------------------------------

export interface ParsedSseChunk {
  /** event 行（无 event 行的匿名消息为 null）。 */
  event: string | null;
  /** data 反序列化结果；[DONE] 原样保留字符串。 */
  data: unknown;
  raw: string;
}

export function parseSseChunks(chunks: string[]): ParsedSseChunk[] {
  return chunks.map((raw) => {
    const eventMatch = raw.match(/^event: (.+)$/m);
    const dataMatch = raw.match(/^data: (.*)$/m);
    const dataRaw = dataMatch ? dataMatch[1] : '';
    return {
      event: eventMatch ? eventMatch[1] : null,
      data: dataRaw === '[DONE]' ? '[DONE]' : JSON.parse(dataRaw),
      raw,
    };
  });
}

export function makeEmitCapture() {
  const chunks: string[] = [];
  return {
    chunks,
    emit: (chunk: string) => {
      chunks.push(chunk);
    },
    events: () => parseSseChunks(chunks),
    /** 匿名 data 消息里按 type 过滤（content 片段的 type 为 undefined）。 */
    byType: (type: string) =>
      parseSseChunks(chunks).filter(
        (c) => typeof c.data === 'object' && c.data !== null &&
          (c.data as Record<string, unknown>).type === type,
      ),
    textDeltas: () =>
      parseSseChunks(chunks)
        .filter((c) => c.event === null && typeof (c.data as { content?: unknown })?.content === 'string' && (c.data as { type?: unknown }).type === undefined)
        .map((c) => (c.data as { content: string }).content),
  };
}

export { FakeLocalStore };
