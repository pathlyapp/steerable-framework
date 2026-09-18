/**
 * sidecar 启动装配（sidecar/boot.ts）行为测试——不起真实 sidecar 进程。
 *
 * boot.ts 是 Electron 主进程与 BS server 共用的装配点：egress 代理决策、
 * supervisor spawn 计划、反向通道接线（tool.invoke / approval.request /
 * ask_user.request / read_state.seed / host.process.spawn）、web 工具与
 * plugin 握手、boot 失败的迟到补接线、shutdown 幂等。这里用模块级 mock
 * 钉住这些决策与接线顺序；真实进程行为归 supervisor.integration.test.ts
 * （opt-in）。
 */
import { EventEmitter } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  supervisorStart: vi.fn(),
  resolveSidecarPython: vi.fn(() => '/venv/bin/python3'),
  setSidecarSupervisor: vi.fn(),
  setSidecarSupervisorPending: vi.fn(),
  llmGetSettings: vi.fn(() => ({ baseUrl: 'https://llm.example/v1', apiKey: 'k' })),
  storeGetWebSearchSettings: vi.fn(async () => undefined as unknown),
  resolveSidecarStoragePath: vi.fn(() => '/tmp/steerable-test/sessions.db'),
  deriveEgressAllowListFromBaseUrl: vi.fn(() => ['llm.example']),
  probeExecSandboxCapability: vi.fn(async () => {}),
  createToolInvokeHandler: vi.fn(() => 'tool-invoke-handler'),
  createProcessSpawnHandler: vi.fn(() => 'spawn-handler'),
  collectAmbientProxyEndpoints: vi.fn(async () => [] as string[]),
  buildEgressProxyPlan: vi.fn(),
  decideEgressProxy: vi.fn(() => ({ start: false, posture: { mode: 'off', reason: null } })),
  deriveWebEgressHosts: vi.fn(() => []),
  pickFreePort: vi.fn(async () => 41000),
  recordEgressPosture: vi.fn(),
  startEgressProxy: vi.fn(),
  webToolsEnabled: vi.fn(() => true),
  sidecarWebSearchEnv: vi.fn(() => ({ STEERABLE_WEB_SEARCH_PROVIDER: 'host' }) as Record<string, string>),
  executeHostedWebSearch: vi.fn(async () => ({ results: [] })),
}));

vi.mock('../../src/sidecar/supervisor.js', () => ({
  SidecarSupervisor: { start: mocks.supervisorStart },
  resolveSidecarPython: mocks.resolveSidecarPython,
}));
vi.mock('../../src/llm/index.js', () => ({
  setSidecarSupervisor: mocks.setSidecarSupervisor,
  setSidecarSupervisorPending: mocks.setSidecarSupervisorPending,
  llmService: { getSettings: mocks.llmGetSettings },
}));
vi.mock('../../src/sidecar/storage-path.js', () => ({
  resolveSidecarStoragePath: mocks.resolveSidecarStoragePath,
}));
vi.mock('../../src/sidecar/exec-sandbox.js', () => ({
  deriveEgressAllowListFromBaseUrl: mocks.deriveEgressAllowListFromBaseUrl,
  probeExecSandboxCapability: mocks.probeExecSandboxCapability,
}));
vi.mock('../../src/sidecar/reverse-tools.js', () => ({
  createToolInvokeHandler: mocks.createToolInvokeHandler,
}));
vi.mock('../../src/sidecar/reverse-spawn.js', () => ({
  createProcessSpawnHandler: mocks.createProcessSpawnHandler,
}));
vi.mock('../../src/sidecar/proxy-detect.js', () => ({
  collectAmbientProxyEndpoints: mocks.collectAmbientProxyEndpoints,
}));
vi.mock('../../src/sidecar/egress-proxy.js', () => ({
  buildEgressProxyPlan: mocks.buildEgressProxyPlan,
  decideEgressProxy: mocks.decideEgressProxy,
  deriveWebEgressHosts: mocks.deriveWebEgressHosts,
  pickFreePort: mocks.pickFreePort,
  recordEgressPosture: mocks.recordEgressPosture,
  startEgressProxy: mocks.startEgressProxy,
}));
vi.mock('../../src/tool-router.js', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../../src/tool-router.js')>();
  return { ...mod, webToolsEnabled: mocks.webToolsEnabled };
});
vi.mock('../../src/storage/web-search-settings.js', () => ({
  sidecarWebSearchEnv: mocks.sidecarWebSearchEnv,
}));
vi.mock('../../src/hosted-web-search.js', () => ({
  executeHostedWebSearch: mocks.executeHostedWebSearch,
}));

import { startHostSidecar, shutdownHostSidecar } from '../../src/sidecar/boot.js';

/** 假 supervisor：EventEmitter（once/emit）+ 测试期可断言的 RPC 桩。 */
function fakeSupervisor(overrides: Record<string, unknown> = {}) {
  const ee = new EventEmitter();
  return Object.assign(ee, {
    onReverseRequest: vi.fn(),
    listToolNames: vi.fn(async () => ['bash', 'web_fetch', 'web_search']),
    call: vi.fn(async () => ({})),
    invokeTool: vi.fn(async () => ({})),
    getBootSnapshot: vi.fn(() => ({ pid: 1 })),
    shutdown: vi.fn(async () => {}),
    ...overrides,
  });
}

function makeDeps(overrides: Record<string, unknown> = {}) {
  const toolRouter = {
    setWebTools: vi.fn(),
    setHostedWebSearch: vi.fn(),
    setPluginRpc: vi.fn(),
  };
  const deps = {
    store: { getWebSearchSettings: mocks.storeGetWebSearchSettings },
    toolRouter,
    resolveProjectRoot: async () => null,
    approvalHandler: 'approval-handler',
    askUserHandler: 'ask-user-handler',
    readStateSeedHandler: 'seed-handler',
    ...overrides,
  };
  return Object.assign(deps, toolRouter) as never;
}

const ENV_KEYS = ['STEERABLE_USE_SIDECAR', 'STEERABLE_EGRESS_PROXY', 'STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS'];
let savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  vi.clearAllMocks();
  // clearAllMocks 只清调用记录、不清 mockReturnValue——默认值在这里重建，
  // 避免上一个测试的定制实现泄漏到下一个。
  mocks.collectAmbientProxyEndpoints.mockResolvedValue([]);
  mocks.decideEgressProxy.mockReturnValue({ start: false, posture: { mode: 'off', reason: null } });
  mocks.webToolsEnabled.mockReturnValue(true);
  mocks.sidecarWebSearchEnv.mockReturnValue({ STEERABLE_WEB_SEARCH_PROVIDER: 'host' });
  mocks.storeGetWebSearchSettings.mockReturnValue(undefined);
  mocks.llmGetSettings.mockReturnValue({ baseUrl: 'https://llm.example/v1', apiKey: 'k' });
  mocks.deriveEgressAllowListFromBaseUrl.mockReturnValue(['llm.example']);
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  delete process.env.STEERABLE_USE_SIDECAR;
  delete process.env.STEERABLE_EGRESS_PROXY;
  delete process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS;
});

afterEach(async () => {
  await shutdownHostSidecar();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
});

describe('startHostSidecar · 开关与 spawn 计划', () => {
  it('STEERABLE_USE_SIDECAR=0 → 完全不启动（opt-out）', async () => {
    process.env.STEERABLE_USE_SIDECAR = '0';
    await startHostSidecar(makeDeps());
    expect(mocks.supervisorStart).not.toHaveBeenCalled();
    expect(mocks.setSidecarSupervisorPending).not.toHaveBeenCalled();
  });

  it('正常启动：storage path 参数、可写根、PTC/run_code 环境、全局句柄与 pending 登记', async () => {
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());

    expect(mocks.supervisorStart).toHaveBeenCalledOnce();
    const plan = mocks.supervisorStart.mock.calls[0][0];
    expect(plan.args).toEqual(['--storage-path', '/tmp/steerable-test/sessions.db']);
    expect(plan.sandboxWritableRoots).toEqual(['/tmp/steerable-test']);
    expect(plan.env).toMatchObject({
      STEERABLE_RUN_CODE: '1',
      STEERABLE_PTC_JS: '1',
      ELECTRON_RUN_AS_NODE: '1',
    });
    expect(plan.env.STEERABLE_PTC_NODE).toBe(process.execPath);
    expect(mocks.setSidecarSupervisor).toHaveBeenCalledWith(sup);
    expect(mocks.setSidecarSupervisorPending).toHaveBeenCalledOnce();
  });

  it('反向通道全接线：tool.invoke / approval / ask_user / read_state.seed / host.process.spawn', async () => {
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());

    const registered = Object.fromEntries(sup.onReverseRequest.mock.calls);
    expect(registered['tool.invoke']).toBe('tool-invoke-handler');
    expect(registered['approval.request']).toBe('approval-handler');
    expect(registered['ask_user.request']).toBe('ask-user-handler');
    expect(registered['read_state.seed']).toBe('seed-handler');
    expect(registered['host.process.spawn']).toBe('spawn-handler');
  });

  it('askUserHandler / readStateSeedHandler 缺省时对应通道不注册', async () => {
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    delete (deps as Record<string, unknown>).askUserHandler;
    delete (deps as Record<string, unknown>).readStateSeedHandler;
    await startHostSidecar(deps);

    const names = sup.onReverseRequest.mock.calls.map((c) => c[0]);
    expect(names).not.toContain('ask_user.request');
    expect(names).not.toContain('read_state.seed');
    expect(names).toContain('tool.invoke');
  });

  it('沙箱出网名单：env 显式名单优先于 baseUrl 派生', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS = 'a.example, b.example';
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());
    expect(mocks.supervisorStart.mock.calls[0][0].sandboxAllowedHosts).toEqual(['a.example', 'b.example']);
  });

  it('沙箱出网名单：baseUrl 派生与 ambient 代理合并去重（W4-8 代理用户不断网）', async () => {
    mocks.collectAmbientProxyEndpoints.mockResolvedValue(['127.0.0.1:7890']);
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());
    expect(mocks.supervisorStart.mock.calls[0][0].sandboxAllowedHosts).toEqual([
      'llm.example',
      '127.0.0.1:7890',
    ]);
  });
});

describe('startHostSidecar · web 工具与插件握手', () => {
  it('web 工具握手：广告名单过滤出 web_fetch/web_search 并接 invoker', async () => {
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await startHostSidecar(deps);

    const setWebTools = (deps as never as { setWebTools: ReturnType<typeof vi.fn> }).setWebTools;
    expect(setWebTools).toHaveBeenCalledOnce();
    const [invoker, names] = setWebTools.mock.calls[0];
    expect(names).toEqual(['web_fetch', 'web_search']);
    // invoker 转发到 supervisor.invokeTool，且 consentGranted（审批已在 sidecar 侧发生）
    await invoker('web_fetch', { url: 'https://x' });
    expect(sup.invokeTool).toHaveBeenCalledWith('web_fetch', { url: 'https://x' },
      expect.objectContaining({ consentGranted: true }));
  });

  it('webToolsEnabled=false → 不询问名单，web 工具缺席', async () => {
    mocks.webToolsEnabled.mockReturnValue(false);
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await startHostSidecar(deps);
    expect(sup.listToolNames).not.toHaveBeenCalled();
    expect((deps as never as { setWebTools: ReturnType<typeof vi.fn> }).setWebTools).toHaveBeenCalledWith(
      expect.any(Function),
      [],
    );
  });

  it('握手失败降级为工具缺席，不阻塞 boot', async () => {
    const sup = fakeSupervisor({
      listToolNames: vi.fn(async () => {
        throw new Error('rpc down');
      }),
    });
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await expect(startHostSidecar(deps)).resolves.toBeUndefined();
    expect((deps as never as { setWebTools: ReturnType<typeof vi.fn> }).setWebTools).not.toHaveBeenCalled();
  });

  it('hosted web search：web_search 在列 + provider=host + 无 key → 接托管搜索', async () => {
    mocks.sidecarWebSearchEnv.mockReturnValue({ STEERABLE_WEB_SEARCH_PROVIDER: 'host' });
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await startHostSidecar(deps);

    const setHosted = (deps as never as { setHostedWebSearch: ReturnType<typeof vi.fn> }).setHostedWebSearch;
    expect(setHosted).toHaveBeenCalledWith(expect.any(Function));
    const fn = setHosted.mock.calls[0][0];
    await fn({ query: 'q', max_results: 3 });
    expect(mocks.executeHostedWebSearch).toHaveBeenCalledWith('q', 3, expect.objectContaining({ baseUrl: 'https://llm.example/v1' }));
  });

  it('hosted web search：有 key 时不接托管（sidecar 自己调搜索后端）', async () => {
    mocks.sidecarWebSearchEnv.mockReturnValue({
      STEERABLE_WEB_SEARCH_PROVIDER: 'host',
      STEERABLE_WEB_SEARCH_API_KEY: 'k',
    });
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await startHostSidecar(deps);
    expect((deps as never as { setHostedWebSearch: ReturnType<typeof vi.fn> }).setHostedWebSearch).toHaveBeenCalledWith(null);
  });

  it('plugin.list 成功 → setPluginRpc 接直调缝；失败 → null 且 boot 继续', async () => {
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    const deps = makeDeps();
    await startHostSidecar(deps);
    const setPluginRpc = (deps as never as { setPluginRpc: ReturnType<typeof vi.fn> }).setPluginRpc;
    expect(setPluginRpc).toHaveBeenCalledWith(expect.any(Function));
    const rpc = setPluginRpc.mock.calls[0][0];
    await rpc('plugin.get', { id: 'p1' });
    expect(sup.call).toHaveBeenCalledWith('plugin.get', { id: 'p1' });

    // 失败分支
    vi.clearAllMocks();
    const sup2 = fakeSupervisor({
      call: vi.fn(async () => {
        throw new Error('no registry');
      }),
    });
    mocks.supervisorStart.mockResolvedValue(sup2);
    const deps2 = makeDeps();
    await expect(startHostSidecar(deps2)).resolves.toBeUndefined();
    expect((deps2 as never as { setPluginRpc: ReturnType<typeof vi.fn> }).setPluginRpc).toHaveBeenCalledWith(null);
  });
});

describe('startHostSidecar · egress 代理', () => {
  it('decideEgressProxy 不启动（ambient 代理）→ 端口级管控，记录态势', async () => {
    mocks.collectAmbientProxyEndpoints.mockResolvedValue(['127.0.0.1:7890']);
    mocks.decideEgressProxy.mockReturnValue({ start: false, posture: { mode: 'port-only-fallback', reason: 'ambient' } });
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());

    expect(mocks.startEgressProxy).not.toHaveBeenCalled();
    expect(mocks.recordEgressPosture).toHaveBeenCalledWith({ mode: 'port-only-fallback', reason: 'ambient' });
    // 回退路径：sandboxAllowedHosts 走 baseUrl 派生 + ambient 合并
    expect(mocks.supervisorStart.mock.calls[0][0].sandboxAllowedHosts).toContain('127.0.0.1:7890');
  });

  it('代理启动成功：env 写 HTTPS_PROXY + CONFINED 标记，沙箱名单来自 plan', async () => {
    mocks.decideEgressProxy.mockReturnValue({ start: true, posture: { mode: 'per-host-proxy', reason: null } });
    mocks.buildEgressProxyPlan.mockReturnValue({
      proxyEndpoint: 'http://127.0.0.1:41000',
      proxyUrl: 'http://127.0.0.1:41000',
      proxiedHosts: ['llm.example'],
      sandboxAllowedHosts: ['proxy.local'],
      broker: null,
      control: null,
    });
    mocks.startEgressProxy.mockResolvedValue({ stop: vi.fn(), controlPort: null });
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());

    const plan = mocks.supervisorStart.mock.calls[0][0];
    expect(plan.sandboxAllowedHosts).toEqual(['proxy.local']);
    expect(plan.env).toMatchObject({
      HTTPS_PROXY: 'http://127.0.0.1:41000',
      https_proxy: 'http://127.0.0.1:41000',
      STEERABLE_EGRESS_CONFINED: '1',
    });
    // 代理模式下不开 web-egress（*:80/443 会架空按主机名单），但放行 resolver
    expect(plan.sandboxWebEgress).toBe(false);
    expect(plan.sandboxAllowResolver).toBe(true);
    expect(mocks.recordEgressPosture).toHaveBeenCalledWith({ mode: 'per-host-proxy', reason: null });
  });

  it('代理启动抛错 → 回退端口级管控，boot 继续（加固不能弄断 LLM 通路）', async () => {
    mocks.decideEgressProxy.mockReturnValue({ start: true, posture: { mode: 'per-host-proxy', reason: null } });
    mocks.buildEgressProxyPlan.mockReturnValue({
      proxyEndpoint: 'http://127.0.0.1:41000',
      proxyUrl: 'http://127.0.0.1:41000',
      proxiedHosts: ['llm.example'],
      sandboxAllowedHosts: ['proxy.local'],
      broker: null,
      control: null,
    });
    mocks.startEgressProxy.mockRejectedValue(new Error('proxy spawn failed'));
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await expect(startHostSidecar(makeDeps())).resolves.toBeUndefined();
    expect(mocks.recordEgressPosture).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'port-only-fallback' }),
    );
    // 回退后 env 不带 CONFINED 标记（sidecar 不会误以为自己被收敛）
    expect(mocks.supervisorStart.mock.calls[0][0].env.STEERABLE_EGRESS_CONFINED).toBeUndefined();
  });

  it('派生不出代理 plan → 端口级回退', async () => {
    mocks.decideEgressProxy.mockReturnValue({ start: true, posture: { mode: 'per-host-proxy', reason: null } });
    mocks.buildEgressProxyPlan.mockReturnValue(null);
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());
    expect(mocks.startEgressProxy).not.toHaveBeenCalled();
    expect(mocks.recordEgressPosture).toHaveBeenCalledWith(
      expect.objectContaining({ mode: 'port-only-fallback' }),
    );
  });
});

describe('startHostSidecar · boot 失败与迟到补接线', () => {
  it('start 拒绝且无 supervisor → setSidecarSupervisor(null)，不抛', async () => {
    mocks.supervisorStart.mockRejectedValue(new Error('spawn ENOENT'));
    await expect(startHostSidecar(makeDeps())).resolves.toBeUndefined();
    expect(mocks.setSidecarSupervisor).toHaveBeenCalledWith(null);
  });

  it('start 失败但带 supervisor（SidecarBootFailure）→ 迟到 ready 时补接线', async () => {
    const late = fakeSupervisor();
    const failure = Object.assign(new Error('first boot failed'), { supervisor: late });
    mocks.supervisorStart.mockRejectedValue(failure);
    await startHostSidecar(makeDeps());
    expect(mocks.setSidecarSupervisor).toHaveBeenCalledWith(null);
    expect(late.onReverseRequest).not.toHaveBeenCalled();

    late.emit('ready');
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));
    expect(mocks.setSidecarSupervisor).toHaveBeenCalledWith(late);
    expect(late.onReverseRequest).toHaveBeenCalledWith('tool.invoke', 'tool-invoke-handler');
  });
});

describe('shutdownHostSidecar', () => {
  it('停 supervisor 并幂等；未启动时调用安全', async () => {
    await shutdownHostSidecar();

    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());
    await shutdownHostSidecar();
    expect(sup.shutdown).toHaveBeenCalledOnce();
    expect(mocks.setSidecarSupervisor).toHaveBeenLastCalledWith(null);

    await shutdownHostSidecar();
    expect(sup.shutdown).toHaveBeenCalledOnce();
  });

  it('egress 代理在 shutdown 时一并停止', async () => {
    const stop = vi.fn();
    mocks.decideEgressProxy.mockReturnValue({ start: true, posture: { mode: 'per-host-proxy', reason: null } });
    mocks.buildEgressProxyPlan.mockReturnValue({
      proxyEndpoint: 'http://127.0.0.1:41000',
      proxyUrl: 'http://127.0.0.1:41000',
      proxiedHosts: [],
      sandboxAllowedHosts: [],
      broker: null,
      control: null,
    });
    mocks.startEgressProxy.mockResolvedValue({ stop, controlPort: null });
    const sup = fakeSupervisor();
    mocks.supervisorStart.mockResolvedValue(sup);
    await startHostSidecar(makeDeps());
    await shutdownHostSidecar();
    expect(stop).toHaveBeenCalledOnce();
  });
});
