/**
 * HostRuntime（host/runtime.ts）装配与生命周期测试。
 *
 * 这是 CS（Electron）与 BS（headless server）共享的组合根——历史上两处
 * 各自装配导致 BS 漏接 TaskService。钉住的契约：
 *  - 所有服务构造并接线（终端事件 → 广播；任务服务挂到 router）；
 *  - 启动时恢复持久化的命令默认超时；
 *  - start() 幂等、清扫崩溃遗留的 running 任务、sidecar/终端预热不阻塞；
 *  - read_state.seed 处理器校验入参形状并返回 seeded 计数；
 *  - shutdown 逆序停包装配、杀终端、停 sidecar。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  setDefaultExecTimeoutMs: vi.fn(),
  executorInit: vi.fn(async () => {}),
  seedReadState: vi.fn(() => 0),
  getLlmSettings: vi.fn((): unknown => undefined),
  failRunningTasks: vi.fn(() => 0),
  getChat: vi.fn((): unknown => undefined),
  getPackDb: vi.fn(() => ({})),
  recordUsageEvent: vi.fn(),
  applyPackMigrations: vi.fn(),
  terminalOn: vi.fn(),
  terminalKillAll: vi.fn(),
  terminalEnsurePrimary: vi.fn(() => ({ id: 'main' })),
  startHostSidecar: vi.fn(async () => {}),
  shutdownHostSidecar: vi.fn(async () => {}),
  bindWorkspaceSkillRoots: vi.fn(),
  recordInsightTurn: vi.fn(),
  projectGet: vi.fn((): unknown => undefined),
  refreshAllEnabled: vi.fn(async () => {}),
  setTaskServices: vi.fn(),
  registerToolContributions: vi.fn(),
  listModelSchemas: vi.fn(() => []),
  packAssemblies: new Map<string, (deps: unknown) => unknown>(),
}));

vi.mock('../src/local-executor.js', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../src/local-executor.js')>();
  return {
    ...mod,
    setDefaultExecTimeoutMs: mocks.setDefaultExecTimeoutMs,
    LocalExecutor: class {
      init = mocks.executorInit;
      seedReadState = mocks.seedReadState;
    },
  };
});
vi.mock('../src/local-script-registry.js', () => ({ LocalScriptRegistry: class {} }));
vi.mock('../src/terminal-manager.js', () => ({
  TerminalManager: class {
    on = mocks.terminalOn;
    killAll = mocks.terminalKillAll;
    ensurePrimary = mocks.terminalEnsurePrimary;
  },
}));
vi.mock('../src/tool-router.js', () => ({
  ToolRouter: class {
    setTaskServices = mocks.setTaskServices;
    registerToolContributions = mocks.registerToolContributions;
    listModelSchemas = mocks.listModelSchemas;
  },
  WEB_TOOL_RPC_TIMEOUT_MS: 1000,
  webToolsEnabled: () => false,
}));
vi.mock('../src/mcp-server-registry.js', () => ({
  McpServerRegistry: class {
    refreshAllEnabled = mocks.refreshAllEnabled;
  },
}));
vi.mock('../src/project-registry.js', () => ({
  ProjectRegistry: class {
    get = mocks.projectGet;
  },
}));
vi.mock('../src/local-backend/router.js', () => ({
  LocalBackendRouter: class {
    resolveChatProject = vi.fn(() => null);
  },
}));
vi.mock('../src/local-backend/worktree-service.js', () => ({ WorktreeService: class {} }));
vi.mock('../src/local-backend/task-service.js', () => ({ TaskService: class {} }));
vi.mock('../src/storage/index.js', () => ({
  localStore: {
    getLlmSettings: mocks.getLlmSettings,
    failRunningTasks: mocks.failRunningTasks,
    getChat: mocks.getChat,
    getPackDb: mocks.getPackDb,
    recordUsageEvent: mocks.recordUsageEvent,
    applyPackMigrations: mocks.applyPackMigrations,
  },
}));
vi.mock('../src/sidecar/reverse-approval.js', () => ({
  createApprovalBridge: () => ({ handler: 'approval-handler', decide: vi.fn() }),
}));
vi.mock('../src/sidecar/reverse-ask-user.js', () => ({
  createAskUserBridge: () => ({ handler: 'ask-user-handler', answer: vi.fn() }),
}));
vi.mock('../src/sidecar/boot.js', () => ({
  startHostSidecar: mocks.startHostSidecar,
  shutdownHostSidecar: mocks.shutdownHostSidecar,
}));
vi.mock('../src/host/visible-terminal-exec.js', () => ({
  createVisibleTerminalExec: () => vi.fn(async () => null),
}));
vi.mock('../src/json-store.js', () => ({ createJsonStore: () => ({}) }));
vi.mock('../src/local-backend/skill-loader.js', () => ({
  bindWorkspaceSkillRoots: mocks.bindWorkspaceSkillRoots,
}));
vi.mock('../src/attachments.js', () => ({
  getChatAttachmentsDir: (chatId: string) => `/tmp/attachments/${chatId}`,
  saveAttachmentFiles: vi.fn(),
}));
vi.mock('../src/insights/record.js', () => ({ recordInsightTurn: mocks.recordInsightTurn }));
vi.mock('../src/host/pack-assembly.js', () => ({
  getPackAssemblies: () => mocks.packAssemblies,
}));

import { createHostRuntime } from '../src/host/runtime.js';

function makeOptions(overrides: Record<string, unknown> = {}) {
  return {
    broadcast: vi.fn(),
    hasWindow: () => true,
    onLog: vi.fn(),
    taskSweepReason: '宿主重启，任务中断',
    ...overrides,
  } as never;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.packAssemblies.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('createHostRuntime · 装配', () => {
  it('所有服务就位，任务/工作台服务挂到 toolRouter', () => {
    const rt = createHostRuntime(makeOptions());
    expect(rt.localExecutor).toBeDefined();
    expect(rt.toolRouter).toBeDefined();
    expect(rt.taskService).toBeDefined();
    expect(rt.localBackendRouter).toBeDefined();
    expect(mocks.setTaskServices).toHaveBeenCalledOnce();
    expect(mocks.bindWorkspaceSkillRoots).toHaveBeenCalledOnce();
  });

  it('终端事件转发到广播（data / exit / spawned）', () => {
    const options = makeOptions();
    createHostRuntime(options);
    const handlers = Object.fromEntries(mocks.terminalOn.mock.calls.map((c) => [c[0], c[1]]));
    const broadcast = (options as never as { broadcast: ReturnType<typeof vi.fn> }).broadcast;

    handlers.data('s1', 'chunk');
    expect(broadcast).toHaveBeenCalledWith('terminal:data', { sessionId: 's1', chunk: 'chunk' });
    handlers.exit('s1', 0, null);
    expect(broadcast).toHaveBeenCalledWith('terminal:exit', { sessionId: 's1', code: 0, signal: null });
    handlers.spawned({ id: 's2' });
    expect(broadcast).toHaveBeenCalledWith('terminal:spawned', { id: 's2' });
  });

  it('持久化的 execTimeoutSeconds 恢复为毫秒；未配置时传 null', () => {
    mocks.getLlmSettings.mockReturnValue({ execTimeoutSeconds: 45 });
    createHostRuntime(makeOptions());
    expect(mocks.setDefaultExecTimeoutMs).toHaveBeenCalledWith(45_000);

    vi.clearAllMocks();
    mocks.getLlmSettings.mockReturnValue(undefined);
    createHostRuntime(makeOptions());
    expect(mocks.setDefaultExecTimeoutMs).toHaveBeenCalledWith(null);
  });

  it('包装配：注册表里的包逐个装配，deps 带 db/广播/记录缝；迁移先应用且幂等', () => {
    const assemble = vi.fn((deps: { registerTools: unknown }) => ({ dispose: vi.fn() }));
    mocks.packAssemblies.set('demo-pack', assemble);
    const rt = createHostRuntime(makeOptions());
    expect(mocks.applyPackMigrations).toHaveBeenCalledOnce();
    expect(assemble).toHaveBeenCalledOnce();
    expect(rt.packHandles.has('demo-pack')).toBe(true);
    const deps = assemble.mock.calls[0][0] as Record<string, unknown>;
    for (const key of ['db', 'listTools', 'resolveChatProject', 'broadcast', 'registerTools', 'recordUsage', 'recordInsight', 'onLog']) {
      expect(deps[key], `pack deps 缺 ${key}`).toBeDefined();
    }
  });

  it('包装配返回 null（包自行退出）不占 handle', () => {
    mocks.packAssemblies.set('noop-pack', () => null);
    const rt = createHostRuntime(makeOptions());
    expect(rt.packHandles.size).toBe(0);
  });
});

describe('start · 生命周期', () => {
  it('幂等：第二次调用不再清扫/重启 sidecar', () => {
    const rt = createHostRuntime(makeOptions());
    rt.start();
    rt.start();
    expect(mocks.failRunningTasks).toHaveBeenCalledOnce();
    expect(mocks.startHostSidecar).toHaveBeenCalledOnce();
  });

  it('启动清扫 running 任务并记录原因；有清扫结果时打日志', () => {
    mocks.failRunningTasks.mockReturnValue(3);
    const options = makeOptions();
    const rt = createHostRuntime(options);
    rt.start();
    expect(mocks.failRunningTasks).toHaveBeenCalledWith('宿主重启，任务中断');
    const onLog = (options as never as { onLog: ReturnType<typeof vi.fn> }).onLog;
    expect(onLog).toHaveBeenCalledWith(expect.stringContaining('3'));
  });

  it('sidecar 启动接线：反向通道处理器与附件目录只读根', () => {
    const rt = createHostRuntime(makeOptions());
    rt.start();
    const bootDeps = mocks.startHostSidecar.mock.calls[0][0];
    expect(bootDeps.approvalHandler).toBe('approval-handler');
    expect(bootDeps.askUserHandler).toBe('ask-user-handler');
    expect(bootDeps.resolveAdditionalReadRoots('chat-1')).toEqual(['/tmp/attachments/chat-1']);
  });

  it('read_state.seed 处理器：合法 state 透传并回 seeded；畸形入参按空表处理', async () => {
    mocks.seedReadState.mockReturnValue(5);
    const rt = createHostRuntime(makeOptions());
    rt.start();
    const handler = mocks.startHostSidecar.mock.calls[0][0].readStateSeedHandler;

    expect(await handler({ state: { '/a': 'v1' } })).toEqual({ seeded: 5 });
    expect(mocks.seedReadState).toHaveBeenCalledWith({ '/a': 'v1' });

    mocks.seedReadState.mockClear();
    mocks.seedReadState.mockReturnValue(0);
    expect(await handler({ state: ['not', 'object'] })).toEqual({ seeded: 0 });
    expect(mocks.seedReadState).toHaveBeenCalledWith({});
    expect(await handler(undefined)).toEqual({ seeded: 0 });
  });

  it('MCP 工具列表后台刷新；终端预热在 2s 后且失败只打日志', () => {
    vi.useFakeTimers();
    mocks.terminalEnsurePrimary.mockImplementation(() => {
      throw new Error('pty unavailable');
    });
    const options = makeOptions();
    const rt = createHostRuntime(options);
    rt.start();
    expect(mocks.refreshAllEnabled).toHaveBeenCalledOnce();
    expect(mocks.terminalEnsurePrimary).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2_100);
    expect(mocks.terminalEnsurePrimary).toHaveBeenCalledOnce();
    const onLog = (options as never as { onLog: ReturnType<typeof vi.fn> }).onLog;
    expect(onLog).toHaveBeenCalledWith(expect.stringContaining('pty unavailable'));
  });
});

describe('shutdown', () => {
  it('杀终端、逆序停包装配、停 sidecar', async () => {
    const order: string[] = [];
    mocks.packAssemblies.set('pack-a', () => ({ dispose: () => void order.push('a') }));
    mocks.packAssemblies.set('pack-b', () => ({ dispose: () => void order.push('b') }));
    const rt = createHostRuntime(makeOptions());
    await rt.shutdown();
    expect(mocks.terminalKillAll).toHaveBeenCalledOnce();
    expect(order).toEqual(['b', 'a']);
    expect(mocks.shutdownHostSidecar).toHaveBeenCalledOnce();
  });
});
