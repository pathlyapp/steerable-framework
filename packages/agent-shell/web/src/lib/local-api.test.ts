/**
 * local-api 传输契约：每个 helper 只是把调用翻译成
 * `window.electron.localBackend.request({ method, path, body? })`，自身不含
 * 业务逻辑。这里逐字锁定方法 / 路径 / 查询串 / body 的形状，以及：
 *   - 桥缺失时 fail-loud（调用方应先 isElectron() 判断降级）；
 *   - 路径参数一律 encodeURIComponent（id 里的 `/`、空格不破坏路由）；
 *   - request 拒绝时错误原样抛出（不包装、不吞）；
 *   - 响应按原样透传（泛型只是编译期标注）。
 * 桥走真实的 getElectronBridge() → window.electron 路径，不 mock 模块。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as api from './local-api';

type RequestInput = { method: string; path: string; body?: unknown };

/** 安装最小可用的 window.electron 桥，返回可断言的 request 替身。 */
function installBridge() {
  const request = vi.fn<(input: RequestInput) => Promise<unknown>>();
  (window as { electron?: unknown }).electron = { localBackend: { request } };
  return request;
}

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
});

describe('local-api 桥接前置', () => {
  it('桥缺失时 GET / POST helper 都拒绝并提示只能在桌面壳内使用', async () => {
    await expect(api.listChats()).rejects.toThrow(/Electron bridge unavailable/);
    await expect(api.createChat()).rejects.toThrow(/Electron bridge unavailable/);
  });

  it('request 拒绝时错误原样抛出，不包装', async () => {
    const request = installBridge();
    request.mockRejectedValue(new Error('主进程已退出'));
    await expect(api.listProjects()).rejects.toThrow('主进程已退出');
  });

  it('响应按原样透传给调用方', async () => {
    const request = installBridge();
    const payload = {
      chats: [{ id: 'chat-1' }],
      pagination: { page: 1, limit: 50, total: 1, totalPages: 1, hasMore: false },
    };
    request.mockResolvedValue(payload);
    await expect(api.listChats()).resolves.toEqual(payload);
  });
});

describe('local-api 会话', () => {
  it('listChats 缺省 page=1&limit=50，自定义分页透传', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.listChats();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/chats?page=1&limit=50' });
    await api.listChats({ page: 3, limit: 10 });
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/chats?page=3&limit=10' });
  });

  it('createChat 缺省发空对象 body，带参时透传 agentId / projectId', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.createChat();
    expect(request).toHaveBeenLastCalledWith({ method: 'POST', path: '/api/v2/chats/new', body: {} });
    await api.createChat({ agentId: 'agent-a', projectId: 'proj-1' });
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/chats/new',
      body: { agentId: 'agent-a', projectId: 'proj-1' },
    });
  });

  it('deleteChat 对 chatId 做 URL 编码', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.deleteChat('chat/1?x');
    expect(request).toHaveBeenCalledWith({ method: 'DELETE', path: '/api/v2/chats/chat%2F1%3Fx' });
  });

  it('deleteChatIfEmpty 追加 onlyIfEmpty=1 查询串', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.deleteChatIfEmpty('chat-1');
    expect(request).toHaveBeenCalledWith({
      method: 'DELETE',
      path: '/api/v2/chats/chat-1?onlyIfEmpty=1',
    });
  });

  it('pruneEmptyChats 缺省把 exceptChatId 归一为 null，带参时透传', async () => {
    const request = installBridge();
    request.mockResolvedValue({ deletedChatIds: [] });
    await api.pruneEmptyChats();
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/chats/prune-empty',
      body: { exceptChatId: null },
    });
    await api.pruneEmptyChats('chat-9');
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/chats/prune-empty',
      body: { exceptChatId: 'chat-9' },
    });
  });
});

describe('local-api 会话分支', () => {
  it('getChatBranches / activateChatBranch / getChatBranchTree 的路径与 body', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getChatBranches('chat-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/chats/chat-1/branches' });
    await api.activateChatBranch('chat-1', 'rec-2');
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/chats/chat-1/branches/activate',
      body: { recordId: 'rec-2' },
    });
    await api.getChatBranchTree('chat-1');
    expect(request).toHaveBeenLastCalledWith({
      method: 'GET',
      path: '/api/v2/chats/chat-1/branches/tree',
    });
  });
});

describe('local-api 智能体', () => {
  it('listChatAgents 缺省不含已归档，true 时追加 include_archived=true', async () => {
    const request = installBridge();
    request.mockResolvedValue({ agents: [], total: 0 });
    await api.listChatAgents();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/chat-agents' });
    await api.listChatAgents(true);
    expect(request).toHaveBeenLastCalledWith({
      method: 'GET',
      path: '/api/v2/chat-agents?include_archived=true',
    });
  });

  it('listChatAgentSkills / listChatAgentTools 走各自目录端点', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.listChatAgentSkills();
    await api.listChatAgentTools();
    expect(request).toHaveBeenNthCalledWith(1, { method: 'GET', path: '/api/v2/chat-agents/skills' });
    expect(request).toHaveBeenNthCalledWith(2, { method: 'GET', path: '/api/v2/chat-agents/tools' });
  });

  it('createChatAgent 透传嵌套 body（技能 / 工具策略）', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    const input = {
      name: '调研员',
      skillIds: ['skill-a'],
      toolPolicy: { mode: 'allowlist' as const, tools: ['tool-a'] },
      allowExternalSkills: false,
    };
    await api.createChatAgent(input);
    expect(request).toHaveBeenCalledWith({ method: 'POST', path: '/api/v2/chat-agents', body: input });
  });

  it('updateChatAgent 用 PATCH 且编码 agentId，archiveChatAgent 走 DELETE', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.updateChatAgent('agent/1', { name: '新名字' });
    expect(request).toHaveBeenLastCalledWith({
      method: 'PATCH',
      path: '/api/v2/chat-agents/agent%2F1',
      body: { name: '新名字' },
    });
    await api.archiveChatAgent('agent-1');
    expect(request).toHaveBeenLastCalledWith({
      method: 'DELETE',
      path: '/api/v2/chat-agents/agent-1',
    });
  });
});

describe('local-api 运行中回合', () => {
  it('getChatLiveStream 拉快照，cancelChatTurn 以空 body POST 取消端点', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getChatLiveStream('chat-1');
    expect(request).toHaveBeenLastCalledWith({
      method: 'GET',
      path: '/api/v2/chats/chat-1/live-stream',
    });
    await api.cancelChatTurn('chat-1');
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/chats/chat-1/cancel',
      body: {},
    });
  });
});

describe('local-api 项目', () => {
  it('listProjects / createProject / updateProject / deleteProject 各走 REST 语义', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.listProjects();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/projects' });
    await api.createProject({ name: '项目甲', folderPath: '/tmp/proj-a' });
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/projects',
      body: { name: '项目甲', folderPath: '/tmp/proj-a' },
    });
    await api.updateProject('proj-1', { name: '项目乙' });
    expect(request).toHaveBeenLastCalledWith({
      method: 'PUT',
      path: '/api/v2/projects/proj-1',
      body: { name: '项目乙' },
    });
    await api.deleteProject('proj-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'DELETE', path: '/api/v2/projects/proj-1' });
  });

  it('getChatProjectContext 拉取会话的项目上下文', async () => {
    const request = installBridge();
    request.mockResolvedValue({ project: null });
    await api.getChatProjectContext('chat-1');
    expect(request).toHaveBeenCalledWith({
      method: 'GET',
      path: '/api/v2/chats/chat-1/project-context',
    });
  });

  it('setProjectTrusted 以 PUT body 携带信任旗标', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.setProjectTrusted('proj-1', true);
    expect(request).toHaveBeenCalledWith({
      method: 'PUT',
      path: '/api/v2/projects/proj-1/trust',
      body: { trusted: true },
    });
  });

  it('updateChatProject 走 chat settings PATCH，null 表示移出项目', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.updateChatProject('chat-1', 'proj-1');
    expect(request).toHaveBeenLastCalledWith({
      method: 'PATCH',
      path: '/api/v2/chats/chat-1/settings',
      body: { projectId: 'proj-1' },
    });
    await api.updateChatProject('chat-1', null);
    expect(request).toHaveBeenLastCalledWith({
      method: 'PATCH',
      path: '/api/v2/chats/chat-1/settings',
      body: { projectId: null },
    });
  });
});

describe('local-api 任务', () => {
  it('listChatTasks / getTaskProcess / merge / discard 的路径形状', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.listChatTasks('chat-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/chats/chat-1/tasks' });
    await api.getTaskProcess('task-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/tasks/task-1/process' });
    await api.mergeTaskWorktree('task-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'POST', path: '/api/v2/tasks/task-1/merge' });
    await api.discardTaskWorktree('task-1');
    expect(request).toHaveBeenLastCalledWith({ method: 'POST', path: '/api/v2/tasks/task-1/discard' });
  });
});

describe('local-api LLM 设置与目录', () => {
  it('getCompatFlags / getProviderPresets / getCatalogProviders 各走只读端点', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getCompatFlags();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/compat/flags' });
    await api.getProviderPresets();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/presets' });
    await api.getCatalogProviders();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/catalog' });
  });

  it('resolveProviderPreset 无参时路径以空查询串结尾，带参时做查询编码', async () => {
    const request = installBridge();
    request.mockResolvedValue({ preset: null });
    // 无参时尾部多一个空 `?`（模板字符串无条件拼接）——锁定现状，见报告。
    await api.resolveProviderPreset();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/presets/resolve?' });
    await api.resolveProviderPreset('https://llm.example.test/v1', 'model x');
    expect(request).toHaveBeenLastCalledWith({
      method: 'GET',
      path: '/api/v2/llm/presets/resolve?baseUrl=https%3A%2F%2Fllm.example.test%2Fv1&model=model+x',
    });
  });

  it('getLlmModels 无 draft / 空 draft 时不带查询串', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getLlmModels();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/models' });
    await api.getLlmModels({});
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/models' });
  });

  it('getLlmModels 透传 draft 凭证与 refresh 旗标，refresh=false 不下发', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getLlmModels({
      baseUrl: 'https://llm.example.test/v1',
      apiKey: 'sk-test',
      provider: 'ollama',
      refresh: true,
    });
    expect(request).toHaveBeenLastCalledWith({
      method: 'GET',
      path: '/api/v2/llm/models?baseUrl=https%3A%2F%2Fllm.example.test%2Fv1&apiKey=sk-test&provider=ollama&refresh=1',
    });
    await api.getLlmModels({ refresh: false });
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/llm/models' });
  });

  it('getSidecarSandboxPosture 拉取沙箱态势', async () => {
    const request = installBridge();
    request.mockResolvedValue({ posture: null });
    await api.getSidecarSandboxPosture();
    expect(request).toHaveBeenCalledWith({ method: 'GET', path: '/api/v2/sidecar/sandbox-posture' });
  });

  it('getLlmSettings / setLlmSettings 读写同一端点', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    await api.getLlmSettings();
    expect(request).toHaveBeenLastCalledWith({ method: 'GET', path: '/api/v2/local-settings/llm' });
    const settings = { provider: 'ollama' as const, model: 'test-model' };
    await api.setLlmSettings(settings);
    expect(request).toHaveBeenLastCalledWith({
      method: 'POST',
      path: '/api/v2/local-settings/llm',
      body: settings,
    });
  });

  it('diagnoseLlmConnection 以 POST body 携带连接参数', async () => {
    const request = installBridge();
    request.mockResolvedValue({});
    const input = { baseUrl: 'https://llm.example.test/v1', apiKey: 'sk-test', model: 'test-model' };
    await api.diagnoseLlmConnection(input);
    expect(request).toHaveBeenCalledWith({ method: 'POST', path: '/api/v2/llm/diagnose', body: input });
  });
});
