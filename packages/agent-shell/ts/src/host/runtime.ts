/**
 * HostRuntime —— CS（Electron 主进程）与 BS（headless HTTP server）共享的
 * 服务装配与生命周期。
 *
 * 历史上 main.ts 与 server/index.ts 各自装配同一份服务（LocalExecutor /
 * ToolRouter / TaskService / sidecar …），两处逐行重复；BS 曾漏接
 * TaskService 导致模型退化成 local_exec_shell + nohup 的"假后台"。本模块
 * 把装配收敛为一处，两个入口只保留各自的广播机制（IPC webContents.send
 * vs SSE 总线）与生命周期（Electron app 事件 vs SIGINT/SIGTERM）。
 *
 * 这也是阶段 3 上提框架后 `@steerable/agent-shell` 的核心装配件：场景包
 * （ScenarioPack）的服务/工具贡献在 0.3 逐槽接入本模块。
 */
import {
  LocalExecutor,
  setDefaultExecTimeoutMs,
  type LocalExecRequest,
  type LocalExecResult,
} from '../local-executor.js';
import { LocalScriptRegistry } from '../local-script-registry.js';
import { TerminalManager } from '../terminal-manager.js';
import { ToolRouter } from '../tool-router.js';
import {
  McpServerRegistry,
  type McpServerEntry,
} from '../mcp-server-registry.js';
import { ProjectRegistry, type ProjectRecord } from '../project-registry.js';
import { LocalBackendRouter } from '../local-backend/router.js';
import { WorktreeService } from '../local-backend/worktree-service.js';
import { TaskService } from '../local-backend/task-service.js';
import { localStore } from '../storage/index.js';
import { createApprovalBridge } from '../sidecar/reverse-approval.js';
import { createAskUserBridge } from '../sidecar/reverse-ask-user.js';
import { startHostSidecar, shutdownHostSidecar } from '../sidecar/boot.js';
import { createVisibleTerminalExec } from './visible-terminal-exec.js';
import { createJsonStore } from '../json-store.js';
import { bindWorkspaceSkillRoots } from '../local-backend/skill-loader.js';
import { getChatAttachmentsDir } from '../attachments.js';
import { recordInsightTurn } from '../insights/record.js';
import {
  getPackAssemblies,
  type PackAssemblyDeps,
  type PackAssemblyHandle,
} from './pack-assembly.js';

export interface HostRuntimeOptions {
  /** 面向全部用户面的事件广播（CS=所有窗口 IPC，BS=SSE 总线）。 */
  broadcast: (channel: string, payload: unknown) => void;
  /**
   * 可选的"主窗口限定"广播：CS 的 LocalBackendRouter 历史上只推主窗口
   * （title 是 nice-to-have，启动时序里没拿到主窗就静默丢）。缺省退回
   * broadcast（BS 行为）。
   */
  broadcastMain?: (channel: string, payload: unknown) => void;
  /** 是否有人能应答审批/提问（CS=有窗口，BS=有浏览器连着事件总线）。 */
  hasWindow: () => boolean;
  onLog: (line: string) => void;
  /**
   * 启动清扫 running 任务时写入的失败原因。任务流不跨进程存活，上次
   * 进程留下的 running 是崩溃/重启签名；CS/BS 措辞不同（应用/服务）。
   */
  taskSweepReason: string;
}

export interface HostRuntime {
  localExecutor: LocalExecutor;
  localScriptRegistry: LocalScriptRegistry;
  terminalManager: TerminalManager;
  /** 包装配产物（2.3）：按产品组装根注册的包逐个装配；宿主不感知包名。 */
  packHandles: ReadonlyMap<string, PackAssemblyHandle>;
  mcpServerRegistry: McpServerRegistry;
  projectRegistry: ProjectRegistry;
  toolRouter: ToolRouter;
  worktreeService: WorktreeService;
  taskService: TaskService;
  localBackendRouter: LocalBackendRouter;
  approvalBridge: ReturnType<typeof createApprovalBridge>;
  askUserBridge: ReturnType<typeof createAskUserBridge>;
  /** 可见终端优先的 shell 执行缝（CS 的 local:exec-shell IPC 也用它）。 */
  maybeExecInTerminal: (request: LocalExecRequest) => Promise<LocalExecResult | null>;
  /**
   * 启动后台部分：任务清扫、sidecar 启动、MCP 工具列表刷新、终端预热。
   * 不阻塞调用方的就绪路径（sidecar python import 慢，竞速的回合由
   * router 回退处理）。
   */
  start(): void;
  /** 关停：杀终端、停 mock、关 sidecar。 */
  shutdown(): Promise<void>;
}

export function createHostRuntime(options: HostRuntimeOptions): HostRuntime {
  const { broadcast, hasWindow, onLog } = options;
  const broadcastMain = options.broadcastMain ?? broadcast;

  const localExecutor = new LocalExecutor();
  // 恢复设置界面里配置的"命令默认超时"（保存时由 local-backend router 即时
  // 生效，这里负责进程重启后的恢复）。
  {
    const persistedLlmSettings = localStore.getLlmSettings();
    setDefaultExecTimeoutMs(
      persistedLlmSettings?.execTimeoutSeconds
        ? persistedLlmSettings.execTimeoutSeconds * 1000
        : null,
    );
  }
  const localScriptRegistry = new LocalScriptRegistry();
  const terminalManager = new TerminalManager();
  // 外部 MCP 服务注册表（设置 → MCP 服务里导入），持久化到
  // userData/agent-mcp-servers.json；项目注册表同理（agent-projects.json）。
  const mcpServerRegistry = new McpServerRegistry(
    createJsonStore<{ mcpServers: McpServerEntry[] }>({
      name: 'agent-mcp-servers',
      defaults: { mcpServers: [] },
    }),
  );
  const projectRegistry = new ProjectRegistry(
    createJsonStore<{ projects: ProjectRecord[] }>({
      name: 'agent-projects',
      defaults: { projects: [] },
    }),
  );
  bindWorkspaceSkillRoots(projectRegistry);

  // 终端输出 / 退出 / 新会话 → 广播（CS=所有窗口，BS=SSE 总线）。
  terminalManager.on('data', (sessionId: string, chunk: string) => {
    broadcast('terminal:data', { sessionId, chunk });
  });
  terminalManager.on('exit', (sessionId: string, code: number, signal: string | null) => {
    broadcast('terminal:exit', { sessionId, code, signal });
  });
  terminalManager.on('spawned', (session) => {
    broadcast('terminal:spawned', session);
  });

  const maybeExecInTerminal = createVisibleTerminalExec({
    localExecutor,
    terminalManager,
  });
  const toolRouter = new ToolRouter(
    localExecutor,
    localScriptRegistry,
    async (request: LocalExecRequest): Promise<LocalExecResult> => {
      const viaTerminal = await maybeExecInTerminal(request);
      if (viaTerminal) return viaTerminal;
      return localExecutor.executeShell(request);
    },
    mcpServerRegistry,
    projectRegistry,
  );
  // 4.6a/4.6b：跨 turn 后台任务 + git worktree 隔离。任务流由宿主独立驱动
  // （不绑父 turn 生命周期，见 task-service.ts 模块头）；终态经广播推到
  // 用户面任务面板。
  const resolveChatProjectRoot = (chatId: string): { name: string; folderPath: string } | null => {
    const projectId = localStore.getChat(chatId)?.projectId;
    if (!projectId) return null;
    const project = projectRegistry.get(projectId);
    return project ? { name: project.name, folderPath: project.folderPath } : null;
  };
  // 场景包装配（2.3）：产品组装根（products/<id>/active.ts）在 import 期
  // 把包装配函数注册进 pack-assembly 注册表；这里统一装配。宿主不 import
  // 任何包代码——产品 tsc 编译单元只含自己的包（纯构建期组合）。
  const packHandles = new Map<string, PackAssemblyHandle>();
  {
    const packDeps = {
      db: localStore.getPackDb(),
      listTools: () => toolRouter.listModelSchemas(),
      resolveChatProject: resolveChatProjectRoot,
      broadcast,
      registerTools: (tools) => toolRouter.registerToolContributions(tools),
      recordUsage: (input) => localStore.recordUsageEvent(input),
      recordInsight: (input) => recordInsightTurn(input),
      onLog,
    } satisfies PackAssemblyDeps;
    // 包迁移容错应用（3.2）：包的 import 链可能经 llm/index 等模块提前
    // 触发 storage 单例构造（ESM 深度优先，active.ts 的注册体后于链上
    // 单例求值）——构造时注册表尚空，此处（装配前）补上已注册包迁移。
    // 幂等（按包 id 去重），注册表为空时零开销。
    localStore.applyPackMigrations();
    for (const [packId, assemble] of getPackAssemblies()) {
      const handle = assemble(packDeps);
      if (handle) packHandles.set(packId, handle);
    }
  }
  const worktreeService = new WorktreeService({ resolveProject: resolveChatProjectRoot });
  const taskService = new TaskService({
    store: localStore,
    toolRouter,
    worktreeService,
    resolveChatProject: resolveChatProjectRoot,
    broadcast,
  });
  toolRouter.setTaskServices({ taskService, worktreeService });
  const localBackendRouter = new LocalBackendRouter(toolRouter, {
    taskService,
    broadcast: broadcastMain,
  });

  // W4-1 / W8：sidecar 反向通道的审批桥与提问桥。
  const approvalBridge = createApprovalBridge({
    broadcast,
    hasWindow,
    onLog: (line) => onLog(line),
  });
  const askUserBridge = createAskUserBridge({
    broadcast,
    hasWindow,
    onLog: (line) => onLog(line),
  });

  let started = false;

  return {
    localExecutor,
    localScriptRegistry,
    terminalManager,
    packHandles,
    mcpServerRegistry,
    projectRegistry,
    toolRouter,
    worktreeService,
    taskService,
    localBackendRouter,
    approvalBridge,
    askUserBridge,
    maybeExecInTerminal,

    start(): void {
      if (started) return;
      started = true;

      // 4.6a：上次进程崩溃/强杀留下的 running 任务不是真相——任务流随进程
      // 一起死了，启动时落成 failed，任务面板据此显示"重启中断"。
      const sweptTasks = localStore.failRunningTasks(options.taskSweepReason);
      if (sweptTasks > 0) {
        onLog(`[task] swept ${sweptTasks} stale running task(s)`);
      }
      void localExecutor.init();
      // Not awaited: sidecar boot (python import) must not delay readiness.
      // A turn that races the boot falls back for that turn only.
      void startHostSidecar({
        toolRouter,
        resolveProjectRoot: (chatId) =>
          localBackendRouter.resolveChatProject(chatId)?.folderPath ?? null,
        // 项目模式下文件读写被围栏在项目目录内；会话附件目录是额外放行的
        // 只读根，保证用户上传的文件即使在项目会话里也能被 agent 读回。
        resolveAdditionalReadRoots: (chatId) => [getChatAttachmentsDir(chatId)],
        approvalHandler: approvalBridge.handler,
        askUserHandler: askUserBridge.handler,
        // P2b: resume 时 sidecar 把记录里的读证据推给 LocalExecutor 的
        // readFileState（CC seed_read_state）——进程重启后自动 CAS 仍成立。
        readStateSeedHandler: (params) => {
          const state = (params as { state?: unknown } | undefined)?.state;
          const seeded = localExecutor.seedReadState(
            state && typeof state === 'object' && !Array.isArray(state)
              ? (state as Record<string, unknown>)
              : {},
          );
          return Promise.resolve({ seeded });
        },
        onLogLine: onLog,
      });
      // 后台刷新已启用 MCP 服务的工具列表（连接慢的 server 不阻塞启动）。
      void mcpServerRegistry.refreshAllEnabled();
      // 预热共享可见 PTY：首条 agent 命令不必付 shell 启动成本。
      setTimeout(() => {
        try {
          terminalManager.ensurePrimary();
        } catch (err) {
          onLog(`[terminal] pre-warm failed: ${err instanceof Error ? err.message : String(err)}`);
        }
      }, 2_000);
    },

    async shutdown(): Promise<void> {
      terminalManager.killAll();
      // 包装配逆序关停（后装配的先停）。
      for (const handle of [...packHandles.values()].reverse()) {
        await handle.dispose?.();
      }
      await shutdownHostSidecar();
    },
  };
}
