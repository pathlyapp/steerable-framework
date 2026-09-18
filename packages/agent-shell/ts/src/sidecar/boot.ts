/**
 * sidecar 启动装配（W2/W4/W5 各反向通道 + egress 加固），Electron 主进程
 * （main.ts）与 BS server（server/index.ts）共用一份，避免两条宿主路径
 * 的启动行为漂移。
 *
 * 模块级持有 supervisor / egress proxy 引用，配对的 `shutdownHostSidecar`
 * 由宿主的退出钩子调用（Electron before-quit / BS SIGTERM）。
 */
import path from 'node:path';
import log from 'electron-log';
import { SidecarSupervisor, resolveSidecarPython, type SidecarBootFailure } from './supervisor.js';
import { setSidecarSupervisor, setSidecarSupervisorPending, llmService } from '../llm/index.js';
import type { ScopedStore } from '../storage/scoped-store.js';
import { resolveSidecarStoragePath } from './storage-path.js';
import {
  deriveEgressAllowListFromBaseUrl,
  probeExecSandboxCapability,
} from './exec-sandbox.js';
import { createToolInvokeHandler } from './reverse-tools.js';
import { createProcessSpawnHandler } from './reverse-spawn.js';
import { collectAmbientProxyEndpoints } from './proxy-detect.js';
import {
  buildEgressProxyPlan,
  decideEgressProxy,
  deriveWebEgressHosts,
  pickFreePort,
  recordEgressPosture,
  startEgressProxy,
  type EgressProxyHandle,
} from './egress-proxy.js';
import { WEB_TOOL_RPC_TIMEOUT_MS, webToolsEnabled, type ToolRouter } from '../tool-router.js';
import { sidecarWebSearchEnv } from '../storage/web-search-settings.js';
import { executeHostedWebSearch } from '../hosted-web-search.js';
import type { createApprovalBridge } from './reverse-approval.js';
import type { SidecarReverseHandler } from './types.js';

export interface HostSidecarDeps {
  store: ScopedStore;
  toolRouter: ToolRouter;
  /** W4-2 项目模式围栏：chatId → 项目根（无项目对话返回 null）。 */
  resolveProjectRoot: (chatId: string) => Promise<string | null>;
  /**
   * 项目模式之外额外放行的只读根（会话附件目录等）。文件写入仍只受
   * projectRoot 围栏约束；这些根只放宽 local_read_file 的读取范围。
   */
  resolveAdditionalReadRoots?: (chatId: string) => string[];
  /** W4-1 审批反向通道处理器（宿主审批弹窗的应答入口）。 */
  approvalHandler: ReturnType<typeof createApprovalBridge>['handler'];
  /**
   * W8：sidecar ask_user 工具的 `ask_user.request` 反向调用处理器
   * （renderer 问题卡片应答）。缺省时 ask_user 对宿主 fail-open 为空答。
   */
  askUserHandler?: SidecarReverseHandler;
  /**
   * P2b：sidecar 会话恢复时把记录里的 read-before-write 证据经
   * `read_state.seed` 推给宿主（桌面 LocalExecutor 的 readFileState）。
   * 缺省时宿主无读证据，自动 CAS 退化为仅依赖显式 expectedVersion。
   */
  readStateSeedHandler?: SidecarReverseHandler;
  onLogLine?: (line: string) => void;
}

let egressProxyRef: EgressProxyHandle | null = null;
let supervisorRef: SidecarSupervisor | null = null;

async function deriveSidecarEgressAllowList(): Promise<string[] | undefined> {
  const envList = (process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS ?? '')
    .split(',')
    .map((h) => h.trim())
    .filter(Boolean);
  if (envList.length > 0) return envList;
  const derived = deriveEgressAllowListFromBaseUrl(llmService.getSettings().baseUrl);
  // W4-8: the sidecar's httpx stack honors ambient proxies (env vars, and
  // the macOS system proxy via getproxies). A configured proxy is an
  // effective egress point — without it in the allow-list, the sandbox
  // denies every LLM call for proxy users (found by dogfooding 2026-08-29:
  // system proxy 127.0.0.1:7890 hijacked even 127.0.0.1:11434 because
  // getproxies() does not surface the system bypass list).
  const proxies = await collectAmbientProxyEndpoints();
  const merged = [...new Set([...(derived ?? []), ...proxies])];
  if (merged.length === 0) {
    log.warn('[sidecar] cannot derive egress allow-list from provider baseUrl; outbound stays open');
    return undefined;
  }
  if (proxies.length > 0) {
    log.info(`[sidecar] egress allow-list includes ambient proxy: ${proxies.join(', ')}`);
  }
  return merged;
}

/**
 * W1.3.3（3.1a 起默认开）：把 sidecar 出网收敛到本机
 * steerable_egress_proxy。显式 `STEERABLE_EGRESS_PROXY=0` 退出。
 *
 * 两类自动回退到旧的 Seatbelt 端口级派生路径（加固永远不能弄断 LLM
 * 通路）：
 * - 检测到 ambient/系统代理：代理解析 CONNECT 后直连目标、无上游链，
 *   系统代理劫持类网络开了反而断网（deriveSidecarEgressAllowList 会把
 *   ambient 端点并进允许列表，见 W4-8）。
 * - 启动失败 / 派生不出任何代理条目。
 */
async function startEgressProxyIfEnabled(store: ScopedStore): Promise<{
  sandboxAllowedHosts?: string[];
  env?: NodeJS.ProcessEnv;
} | null> {
  try {
    const ambient =
      process.env.STEERABLE_EGRESS_PROXY === '0' ? [] : await collectAmbientProxyEndpoints();
    const decision = decideEgressProxy({ env: process.env, ambientProxies: ambient });
    if (!decision.start) {
      if (ambient.length > 0) {
        log.warn(
          `[egress-proxy] ambient proxy detected (${ambient.join(', ')}); ` +
            'the per-host proxy dials targets directly and cannot chain upstream — ' +
            'staying on port-level enforcement',
        );
      }
      recordEgressPosture(decision.posture);
      return null;
    }
    const settings = llmService.getSettings();
    const searchSettings = await store.getWebSearchSettings();
    const searchEnv = sidecarWebSearchEnv({
      processEnv: process.env,
      storedApiKey: searchSettings?.apiKey,
      storedProvider: searchSettings?.provider,
      llmBaseUrl: settings.baseUrl,
    });
    const plan = buildEgressProxyPlan({
      pythonExecutable: resolveSidecarPython(),
      port: await pickFreePort(),
      providerBaseUrl: settings.baseUrl,
      providerApiKey: settings.apiKey || undefined,
      // 3.1b/3.1d：web 工具的允许域名与 sidecar 应用层同源，并入代理
      // CONNECT 白名单——web_fetch/web_search 经 HTTPS_PROXY 走代理，
      // 不再是 confined 下的互斥禁用。
      webAllowedHosts: deriveWebEgressHosts({
        webTools: webToolsEnabled(),
        searchEnv,
        env: process.env,
      }),
    });
    if (!plan) {
      log.warn('[egress-proxy] no https endpoint derivable from provider baseUrl; staying on port-level enforcement');
      recordEgressPosture({
        mode: 'port-only-fallback',
        reason: '无法从 provider baseUrl 派生出 https 端点，按主机管控未启用',
      });
      return null;
    }
    egressProxyRef = await startEgressProxy(plan, (line) =>
      log.info('[egress-proxy]', line),
    );
    log.info(
      `[egress-proxy] per-host egress active via ${plan.proxyEndpoint} (allow: ${plan.proxiedHosts.join(', ')}${plan.broker ? `; credential broker for ${plan.broker.host}` : ''})`,
    );
    recordEgressPosture({ mode: 'per-host-proxy', reason: null });
    // 出网拓宽 ask 链路：控制端点端口 + token 注入 sidecar 环境。sidecar
    // 进程在沙箱外；其沙箱子进程的环境经白名单过滤（run_code 的
    // _child_environ），拿不到 token，无法自我拓宽。
    const controlEnv: NodeJS.ProcessEnv =
      plan.control && egressProxyRef.controlPort
        ? {
            STEERABLE_EGRESS_CONTROL_PORT: String(egressProxyRef.controlPort),
            [plan.control.tokenEnv]: plan.control.tokenValue,
          }
        : {};
    return {
      sandboxAllowedHosts: plan.sandboxAllowedHosts,
      // 小写 https_proxy 是 httpx/getproxies 的惯例键；两个都写以兼容
      // 只认大写的栈。broker 模式下 provider baseUrl 被改写成 http，
      // 所以 HTTP_PROXY 也要指到代理（凭证注入在代理侧完成）。
      // STEERABLE_EGRESS_CONFINED=1 标记"出网已收敛到代理"（W5-2）。只在
      // 这条成功路径上设置——启动失败的回退分支不写，sidecar 不会误以为
      // 自己被收敛。
      env: plan.broker
        ? {
            HTTPS_PROXY: plan.proxyUrl,
            https_proxy: plan.proxyUrl,
            HTTP_PROXY: plan.proxyUrl,
            http_proxy: plan.proxyUrl,
            STEERABLE_EGRESS_CONFINED: '1',
            ...controlEnv,
          }
        : {
            HTTPS_PROXY: plan.proxyUrl,
            https_proxy: plan.proxyUrl,
            STEERABLE_EGRESS_CONFINED: '1',
            ...controlEnv,
          },
    };
  } catch (err) {
    log.warn('[egress-proxy] start failed; falling back to port-level enforcement', err);
    egressProxyRef = null;
    recordEgressPosture({
      mode: 'port-only-fallback',
      reason: `按主机代理启动失败（${err instanceof Error ? err.message : String(err)}），已退回端口级`,
    });
    return null;
  }
}

/**
 * Default-on (2026-08-26): the sidecar hosts the CoreLoop, which is the
 * default chat path. Explicit STEERABLE_USE_SIDECAR=0 opts out (in-process
 * TS loop). Boot is registered synchronously so early RPC thin clients
 * (skill-loader et al.) can await it via whenSidecarSupervisor().
 */
export async function startHostSidecar(deps: HostSidecarDeps): Promise<void> {
  if (process.env.STEERABLE_USE_SIDECAR === '0') return;
  // ready 后的完整接线：注册全局 handle + reverse channels + web 工具握手。
  // 正常 boot 路径与「boot 失败后后台 restart 迟到就绪」路径共用。
  const wireSupervisor = async (supervisor: SidecarSupervisor): Promise<void> => {
    supervisorRef = supervisor;
    setSidecarSupervisor(supervisor);

    // A4 reverse channel: the sidecar-hosted CoreLoop asks this process to
    // execute tools (shell / files / MCP live here, not in Python).
    supervisor.onReverseRequest(
      'tool.invoke',
      createToolInvokeHandler({
        toolRouter: deps.toolRouter,
        onBlocked: (info) =>
          log.warn('[sidecar tool] blocked critical shell command', info),
        // W4-2: project-mode fence on the CoreLoop path — the sidecar's
        // tool.invoke carries context.chatId; resolve it to the project
        // root so ToolRouter's cwd/path confinement applies.
        resolveProjectRoot: deps.resolveProjectRoot,
        resolveAdditionalReadRoots: deps.resolveAdditionalReadRoots,
      }),
    );

    // W4-1: approval algebra reverse channel — the sidecar's
    // ApprovalExecutor(HostApprover) asks here; the renderer modal answers
    // via the host's decide endpoint (IPC in Electron, HTTP in BS).
    supervisor.onReverseRequest('approval.request', deps.approvalHandler);

    // W8: structured user questions — the sidecar-hosted ask_user tool asks
    // here; the renderer question card answers via the host's answer
    // endpoint (IPC in Electron, HTTP in BS).
    if (deps.askUserHandler) {
      supervisor.onReverseRequest('ask_user.request', deps.askUserHandler);
    }

    // P2b: on resume the sidecar rebuilds read-before-write evidence from
    // the durable record and pushes it here so the host's readFileState
    // survives process restarts (CC seed_read_state parity).
    if (deps.readStateSeedHandler) {
      supervisor.onReverseRequest('read_state.seed', deps.readStateSeedHandler);
    }

    // W4.1.1: confined-spawn reverse channel (Windows). The sidecar routes
    // here only when no local rewriter backend exists; the handler confines
    // via win-spawn-helper (restricted token + Job Object) and fails closed
    // on other platforms or when the helper is missing.
    supervisor.onReverseRequest('host.process.spawn', createProcessSpawnHandler());

    // Layer-3 态势：问出这台机器的逐 exec 后端真能达到哪一档强制，
    // `buildExecSandbox` 据此决定要不要 requireFull（见 exec-sandbox.ts）。
    await probeExecSandboxCapability(supervisor);

    // W5-2: web_search/web_fetch handshake. The single implementation lives
    // in the sidecar (web_tools.py); the host router carries schemas only
    // and forwards over `tool.invoke`. tool.list is the availability source
    // of truth — web_search registers only when a search backend is
    // configured, so an unconfigured deployment never advertises a broken
    // tool. Handshake failure degrades to "web tools absent", not a boot
    // failure.
    try {
      const advertised = webToolsEnabled() ? await supervisor.listToolNames() : [];
      const webNames = advertised.filter(
        (n): n is 'web_fetch' | 'web_search' => n === 'web_fetch' || n === 'web_search',
      );
      deps.toolRouter.setWebTools(
        (name, args) =>
          supervisor.invokeTool(name, args, {
            // Approval already happened in the sidecar's CoreLoop
            // (ApprovalExecutor → approval.request reverse channel) before
            // the call was delegated here.
            consentGranted: true,
            timeoutMs: WEB_TOOL_RPC_TIMEOUT_MS,
          }),
        webNames,
      );
      const searchSettings = await deps.store.getWebSearchSettings();
      const searchEnv = sidecarWebSearchEnv({
        processEnv: process.env,
        storedApiKey: searchSettings?.apiKey,
        storedProvider: searchSettings?.provider,
        llmBaseUrl: llmService.getSettings().baseUrl,
      });
      if (
        webNames.includes('web_search')
        && searchEnv.STEERABLE_WEB_SEARCH_PROVIDER === 'host'
        && !searchEnv.STEERABLE_WEB_SEARCH_API_KEY
      ) {
        deps.toolRouter.setHostedWebSearch(async (args) => {
          const query = typeof args.query === 'string' ? args.query : '';
          const max =
            typeof args.max_results === 'number' ? args.max_results : 8;
          return executeHostedWebSearch(query, max, llmService.getSettings());
        });
      } else {
        deps.toolRouter.setHostedWebSearch(null);
      }
      log.info(`[sidecar] web tools available: ${webNames.join(', ') || '(none)'}`);
    } catch (err) {
      log.warn('[sidecar] web tool handshake failed; web tools unavailable this session', err);
    }

    // 插件生命周期：sidecar 的 plugin.* RPC 由 PluginRegistry 背书（__main__
    // 缺省接线）。模型面是 tool-router 的 plugin_* 工具（deferred 层，经
    // tool_search 发现）；这里注入直调缝。握手失败降级为工具缺席，不阻塞 boot。
    try {
      await supervisor.call('plugin.list');
      deps.toolRouter.setPluginRpc((method, params) => supervisor.call(method, params));
      log.info('[sidecar] plugin registry wired; plugin_* tools available');
    } catch (err) {
      deps.toolRouter.setPluginRpc(null);
      log.warn('[sidecar] plugin registry unavailable; plugin_* tools hidden this session', err);
    }

    log.info('[sidecar] ready', supervisor.getBootSnapshot());
  };
  const boot = (async (): Promise<SidecarSupervisor | null> => {
    try {
      const egressProxy = await startEgressProxyIfEnabled(deps.store);
      // W2.6.1: durable sessions/traces/history in a zero-dependency
      // sqlite database under ~/.steerable — the one root the Seatbelt
      // profile already allows writes to; userData would be denied.
      // Path is per-flavor / env-overridable — see resolveSidecarStoragePath.
      const storagePath = resolveSidecarStoragePath();
      const supervisor = await SidecarSupervisor.start({
        args: [
          '--storage-path',
          storagePath,
        ],
        // 当 storage path 落在 ~/.steerable 之外（BS 模式 DEEPPATH_USER_DATA_DIR、
        // 测试 tmpdir、显式 STEERABLE_SIDECAR_STORAGE_PATH）时，沙箱必须显式
        // 授予该目录写权限，否则 sidecar 打不开 sessions.db/sessions.lock。
        sandboxWritableRoots: [path.dirname(storagePath)],
        // W4-3: sandbox default-on. This list is fixed at spawn (the sandbox
        // profile cannot be edited afterwards), so a provider change reaches it
        // only on restart — plain-http endpoints therefore still need one. The
        // proxy's own CONNECT list does track the setting at runtime, via
        // allowEgressForBaseUrl.
        sandboxAllowedHosts:
          egressProxy?.sandboxAllowedHosts ?? (await deriveSidecarEgressAllowList()),
        // egress-proxy 模式下不开 web-egress（*:80/443 会架空代理的按主机
        // 名单）：web 工具改走代理（HTTPS_PROXY），代理白名单与 sidecar
        // 应用层域名名单同源（3.1b/3.1d）。
        sandboxWebEgress: webToolsEnabled() && !egressProxy,
        // 3.1b：代理模式下 web_fetch 的 SSRF 预检仍在沙箱内做 DNS（抓取
        // 本身走代理隧道），所以只放行解析器 socket——无 IP 可达性。
        sandboxAllowResolver: webToolsEnabled() && Boolean(egressProxy),
        env: {
          ...egressProxy?.env,
          ...sidecarWebSearchEnv({
            processEnv: process.env,
            storedApiKey: (await deps.store.getWebSearchSettings())?.apiKey,
            storedProvider: (await deps.store.getWebSearchSettings())?.provider,
            llmBaseUrl: llmService.getSettings().baseUrl,
          }),
          // P1: offer the sidecar's run_code (programmatic tool calls) to the
          // model. The sidecar registers + advertises it; the desktop's
          // tool-router forwards the call back over the reverse channel.
          STEERABLE_RUN_CODE: '1',
          // P3: conversational JS PTC (run_js/wait_js). The sidecar spawns a
          // long-lived Node worker; in the desktop that worker is the bundled
          // runtime — process.execPath is the Electron binary, which runs as
          // plain Node under ELECTRON_RUN_AS_NODE (and already *is* plain
          // Node in headless/BS mode, where the variable is ignored). The
          // sidecar's env scrub passes both through to the worker child.
          STEERABLE_PTC_JS: '1',
          STEERABLE_PTC_NODE: process.execPath,
          ELECTRON_RUN_AS_NODE: '1',
        },
        onLogLine: (line) => (deps.onLogLine ?? ((l) => log.info('[sidecar]', l)))(line),
      });
      await wireSupervisor(supervisor);
      return supervisor;
    } catch (err) {
      log.error('[sidecar] failed to start, falling back to in-process providers', err);
      setSidecarSupervisor(null);
      // boot 失败后 supervisor 的 exit→restart 循环仍在后台重试（另一个
      // 宿主暂时持有 sessions.lock 这类瞬态冲突会自愈）。迟到就绪时补
      // 接线，否则 sidecar 进程活着但 router 永远 503。
      const late = (err as SidecarBootFailure).supervisor;
      if (late) {
        late.once('ready', () => {
          log.info('[sidecar] late ready after initial boot failure');
          void wireSupervisor(late);
        });
      }
      return null;
    }
  })();
  setSidecarSupervisorPending(boot);
  await boot;
}

/** 宿主退出钩子：停 supervisor + egress proxy。幂等。 */
export async function shutdownHostSidecar(): Promise<void> {
  if (supervisorRef) {
    const supervisor = supervisorRef;
    supervisorRef = null;
    setSidecarSupervisor(null);
    await supervisor.shutdown();
  }
  if (egressProxyRef) {
    egressProxyRef.stop();
    egressProxyRef = null;
  }
}
