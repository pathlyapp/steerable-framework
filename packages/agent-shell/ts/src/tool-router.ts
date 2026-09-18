import os from 'node:os';
import path from 'node:path';
import {
  LocalExecutor,
  buildProjectRootViolation,
  isPathWithinRoot,
  type LocalExecRequest,
  type LocalExecResult,
} from './local-executor.js';
import { LocalScriptRegistry } from './local-script-registry.js';
import { mcpExecutor, type McpServerConfig } from './mcp-executor.js';
import type { McpServerRegistry } from './mcp-server-registry.js';
import type { ProjectRegistry } from './project-registry.js';
import type { TaskService } from './local-backend/task-service.js';
import type { WorktreeService } from './local-backend/worktree-service.js';
import type { ToolContribution, ToolMode, ToolExposure } from './scenario/pack.js';

export type { ToolMode, ToolExposure } from './scenario/pack.js';
import {
  rankTools,
  resolveMaxResults,
  TOOL_SEARCH_DEFAULT_MAX_RESULTS,
} from './tool-search-rank.js';
import { maybeAutoInstallWrittenSkill } from './local-backend/skill-install.js';
import {
  filterToolsByPolicy,
  isToolAllowed,
  type AgentToolPolicy,
} from './local-backend/agent-capability.js';

/** 已注册 MCP 服务的动态工具名前缀：`mcp__<serverKey>__<toolName>`。 */
export const MCP_DYNAMIC_TOOL_PREFIX = 'mcp__';

/**
 * 工具曝光分层（steerable-framework Wave 2 同构）：`direct` 进模型可见列表；
 * `deferred` 可分发、可经 `tool_search` 发现，但不占每轮工具列表的 token；
 * `hidden` 仅可分发。缺省 `direct`。
 *
 * ToolMode / ToolExposure 的单一真源在 scenario/pack.ts（pack-sdk），
 * 本模块 re-export 兼容既有调用方。
 */

/** deferred 层的发现缝：一个 direct 层搜索工具，关键词命中即调。 */
export const TOOL_SEARCH_TOOL_NAME = 'tool_search';

export interface ToolCall {
  name: string;
  arguments: Record<string, unknown>;
}

/**
 * 单次工具执行的调用上下文（按 chat 解析，随每次 execute 传入）。
 *
 * `projectRoot` 是项目模式的硬沙箱根：非空时
 *   - local_read_file / local_write_file 的路径必须落在根内（LocalExecutor 围栏）
 *   - local_exec_shell 的 cwd 默认改为项目根；显式给的 cwd 越界则直接拒绝
 * 无项目对话（null/缺省）行为与之前完全一致。
 */
export interface ToolExecContext {
  projectRoot?: string | null;
  /**
   * 项目模式之外额外放行的只读根（会话附件目录等）。只放宽
   * local_read_file 的读取范围，写入/编辑仍只受 projectRoot 围栏约束。
   */
  additionalReadRoots?: string[] | null;
  /**
   * 4.6a：调用发生的 chat。task_run / task_status / task_result 需要它把
   * 任务绑定到来源对话（任务表按 chatId 归组）；reverse-tools 从
   * toolContext.chatId 透传。缺省（CLI/直接 exec 场景）时任务工具拒绝服务。
   */
  chatId?: string;
  /**
   * 本轮智能体的工具准入策略（「智能体管理」页配置，router 按本轮生效的
   * 智能体解析后随 toolContext 下发）。分发层按它复检：被拒的工具即使模型
   * 从历史里捞到名字直接调用也会被拒绝——与 plan 模式同款的双层执行
   * （广告层不出 + 分发层拒绝）。缺省 = 不限制。
   */
  toolPolicy?: AgentToolPolicy | null;
}

export interface ToolSchema {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  mode: ToolMode;
  /** 曝光层；缺省 `direct`。MCP 动态工具默认 `deferred`。 */
  exposure?: ToolExposure;
}

type ShellExecutor = (request: LocalExecRequest) => Promise<LocalExecResult>;

/**
 * web_search / web_fetch 的执行缝（W5-2）：唯一实现在 sidecar 的
 * `web_tools.py`（SSRF 策略、字节上限、重定向上限都在那里），宿主只持
 * schema 并经前向 `tool.invoke` 前转——与 local_* 工具"两处实现、契约
 * 对齐"的模式相反，网络读取对刻意单实现，因此不进 tool-contract。
 * main.ts 在 sidecar 启动后用 `tool.list` 握手填充可用集。
 */
export type WebToolDelegate = (
  name: 'web_search' | 'web_fetch',
  args: Record<string, unknown>,
) => Promise<unknown>;

/** 前转 RPC 超时：sidecar 侧单次请求上限默认 30s（可配到 600s），这里给足余量。 */
export const WEB_TOOL_RPC_TIMEOUT_MS = 130_000;

/**
 * 插件生命周期 RPC 缝：sidecar 的 `plugin.*` 方法（list/enable/disable/reload）
 * 由 PluginRegistry 背书。宿主只持 schema 并经此 delegate 直调——与 web 工具
 * 不同，这些是普通 RPC 而非 tool.invoke（不经过模型工具分发层）。
 */
export type PluginRpcDelegate = (
  method: 'plugin.list' | 'plugin.enable' | 'plugin.disable' | 'plugin.reload',
  params?: Record<string, unknown>,
) => Promise<unknown>;

/**
 * 本机是否提供网络读取工具（`STEERABLE_WEB_TOOLS=0` 关闭）。
 *
 * 同一个开关同时管住两件事：`tool.list` 握手是否把 web 工具装进本路由，
 * 以及 layer-1 Seatbelt profile 是否放行它们所需的出网（解析器 + 80/443）。
 * 两者必须同源——只放宽不暴露是白开的口子，只暴露不放宽则每次抓取都死在
 * 名字解析上，而模型会把它解释成用户断网。
 */
export function webToolsEnabled(): boolean {
  return process.env.STEERABLE_WEB_TOOLS !== '0';
}

export class ToolRouter {
  constructor(
    private readonly localExecutor: LocalExecutor,
    private readonly localScriptRegistry: LocalScriptRegistry,
    private readonly shellExecutor?: ShellExecutor,
    /** 可选：已注册的外部 MCP 服务（设置界面导入）。有缓存工具的服务会追加为一等工具。 */
    readonly mcpRegistry?: McpServerRegistry,
    /** 可选：项目注册表（项目模式）。路由层用它把 chat.projectId 解析成沙箱根目录。 */
    readonly projectRegistry?: ProjectRegistry
  ) {}

  /**
   * 场景包贡献的工具（0.3a 开槽）。schema 进 listSchemas，分发在 execute
   * 的 default 分支——内置工具优先，名字冲突在注册时响亮失败。
   */
  private readonly contributedTools = new Map<string, ToolContribution>();

  /**
   * 注册一组场景工具贡献。与既有工具（内置 / 任务 / web / MCP / 插件 /
   * 已注册贡献）重名即抛错——一个包装不上却静默丢工具是能力暗损。
   */
  registerToolContributions(contributions: readonly ToolContribution[]): void {
    const existing = new Set(this.listSchemas().map((s) => s.name));
    for (const contribution of contributions) {
      if (existing.has(contribution.name)) {
        throw new Error(`[tool-router] duplicate tool name from contribution: ${contribution.name}`);
      }
      this.contributedTools.set(contribution.name, contribution);
      existing.add(contribution.name);
    }
  }

  /**
   * sidecar 握手得到的网络读取工具可用集。web_search 只在 sidecar 配好
   * 搜索后端（设置页免费 DuckDuckGo / Tavily 钥 / STEERABLE_WEB_SEARCH_API_KEY /
   * TAVILY_API_KEY，或 STEERABLE_WEB_SEARCH_PROVIDER=host）时注册——
   * 宿主不猜，避免出现"工具在列表里但一调就报未配置"。
   */
  private webToolDelegate: WebToolDelegate | null = null;
  private webToolNames = new Set<string>();
  /**
   * OpenAI hosted search executed in this process with the chat credential.
   * Set when the sidecar registered `web_search` via provider=host so the
   * host must not forward the call back into the sidecar.
   */
  private hostedWebSearch: ((args: Record<string, unknown>) => Promise<unknown>) | null = null;

  /**
   * 4.6a/4.6b：跨 turn 任务与 git worktree 服务。setter 注入（与
   * setWebTools 同模式）——main.ts 里 ToolRouter 先于 TaskService 构造
   * （TaskService 反向依赖 ToolRouter 的工具列表），构造后再接线。
   * 未接线时任务/worktree 工具不出场（schema 都不出，模型看不到）。
   */
  private taskService: TaskService | null = null;
  private worktreeService: WorktreeService | null = null;

  setTaskServices(services: { taskService: TaskService; worktreeService: WorktreeService } | null): void {
    this.taskService = services?.taskService ?? null;
    this.worktreeService = services?.worktreeService ?? null;
  }

  setWebTools(delegate: WebToolDelegate | null, names: readonly string[] = []): void {
    this.webToolDelegate = delegate;
    this.webToolNames = new Set(names);
  }

  setHostedWebSearch(
    handler: ((args: Record<string, unknown>) => Promise<unknown>) | null,
  ): void {
    this.hostedWebSearch = handler;
  }

  /**
   * sidecar 握手后注入插件 RPC 缝。未注入（sidecar 缺席/握手失败）时
   * plugin_* 工具一个 schema 都不出——模型看不到不可用的工具。
   */
  private pluginRpc: PluginRpcDelegate | null = null;
  setPluginRpc(delegate: PluginRpcDelegate | null): void {
    this.pluginRpc = delegate;
  }

  listSchemas(): ToolSchema[] {
    return [
      {
        name: 'local_exec_shell',
        description: 'Execute a local shell command',
        mode: 'destructive',
        inputSchema: {
          type: 'object',
          properties: {
            command: { type: 'string' },
            cwd: { type: 'string' },
            timeout: { type: 'number', description: 'Timeout in milliseconds (e.g. 30000). If a small number like 15 or 30 is provided, it is assumed to be in seconds and automatically multiplied by 1000.' },
          },
          required: ['command'],
        },
      },
      {
        name: 'local_read_file',
        description:
          'Read a local text file. Large files can be paged with offset/limit (1-based line numbers); ' +
          'a paged or clipped result is marked `partial: true`, and a full-file overwrite of a path ' +
          'only partially read in this session is rejected — use local_edit_file for targeted changes, ' +
          'or finish reading all segments before rewriting.',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            path: { type: 'string' },
            offset: {
              type: 'number',
              description: 'Optional 1-based line number to start reading from (for paging large files).',
            },
            limit: {
              type: 'number',
              description: 'Optional maximum number of lines to return (for paging large files).',
            },
          },
          required: ['path'],
        },
      },
      {
        name: 'local_write_file',
        description: 'Write content to local file path',
        mode: 'destructive',
        inputSchema: {
          type: 'object',
          properties: {
            path: { type: 'string' },
            content: { type: 'string' },
            createDirs: { type: 'boolean' },
            expectedVersion: {
              type: 'string',
              description:
                'Optional. The `version` returned by your last local_read_file of this path. When provided, the write is rejected if the file changed since — prevents overwriting external edits.',
            },
          },
          required: ['path', 'content'],
        },
      },
      {
        name: 'local_edit_file',
        description:
          'Make targeted edits to an existing local file WITHOUT rewriting the whole file. ' +
          'Provide one or more {oldText, newText} edits: each oldText is located in the current file ' +
          '(exact match, then whitespace-tolerant, then Unicode-punctuation-normalized) and replaced by newText. ' +
          'Prefer this over local_write_file for modifying existing files — it is safer (no full-file retype) and cheaper. ' +
          'All edits are matched against the original file, must not overlap, and apply together; if any oldText is not found or ambiguous the whole call fails and nothing is written. ' +
          'For safety, first local_read_file the path, then pass its `version` as expectedVersion so the edit is rejected if the file changed underneath you.',
        mode: 'destructive',
        inputSchema: {
          type: 'object',
          properties: {
            path: { type: 'string', description: 'File path to edit.' },
            edits: {
              type: 'array',
              description: 'One or more targeted replacements, applied together.',
              items: {
                type: 'object',
                properties: {
                  oldText: {
                    type: 'string',
                    description: 'Exact snippet to replace. Include enough surrounding lines to be unique.',
                  },
                  newText: { type: 'string', description: 'Replacement text.' },
                },
                required: ['oldText', 'newText'],
              },
            },
            expectedVersion: {
              type: 'string',
              description:
                'Optional. The `version` returned by your last local_read_file of this path. When provided, the edit is rejected on conflict.',
            },
          },
          required: ['path', 'edits'],
        },
      },
      {
        name: 'local_open_path',
        description: 'Open local path or URL',
        mode: 'local',
        inputSchema: {
          type: 'object',
          properties: {
            target: { type: 'string' },
          },
          required: ['target'],
        },
      },
      {
        // R14 P1 同名物理清理：原名 local_run_code 与 sidecar 的 run_code 同名
        // 并列、模型难辨。更名为 local_run_snippet（跑独立代码片段 + 装依赖，
        // 主机侧、不可链工具），与 run_code（程序化工具调用、受限子进程、可链
        // 工具）职责清晰区分。direct 曝光保留，86-proactive-coding 技能条件不变。
        name: 'local_run_snippet',
        description:
          'Write a code snippet to a scratch file and run it with the local Python/Node interpreter, ' +
          'optionally installing dependencies first (pipPackages / npmPackages). ' +
          'Use this to proactively solve tasks no dedicated tool covers: data processing, format conversion, ' +
          'parsing, computation, batch file operations, automation glue. ' +
          'Returns stdout/stderr/exitCode plus scriptPath (snippet is kept on disk for inspection).',
        mode: 'destructive',
        inputSchema: {
          type: 'object',
          properties: {
            language: {
              type: 'string',
              enum: ['python', 'node'],
              description: 'Interpreter for the snippet.',
            },
            code: { type: 'string', description: 'Complete source code of the snippet.' },
            cwd: {
              type: 'string',
              description:
                'Working directory. Project-bound chats: defaults to the project root and must stay inside it.',
            },
            timeout: {
              type: 'number',
              description:
                'Run timeout in milliseconds (a small number like 30 is treated as seconds). Dependency installs get their own generous timeout.',
            },
            pipPackages: {
              type: 'array',
              items: { type: 'string' },
              description:
                'Python deps to `pip install` before running (e.g. ["pandas", "openpyxl"]). Uses --user, with an automatic venv fallback on PEP 668 systems.',
            },
            npmPackages: {
              type: 'array',
              items: { type: 'string' },
              description:
                'Node deps to `npm install` into the scratch dir before running (e.g. ["xlsx"]). Scripts can require/import them directly.',
            },
          },
          required: ['language', 'code'],
        },
      },
      {
        name: 'local_list_scripts',
        description: 'List all saved local scripts',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {},
        },
      },
      {
        name: 'local_run_script',
        description: 'Run a saved local script by scriptId',
        mode: 'local',
        inputSchema: {
          type: 'object',
          properties: {
            scriptId: { type: 'string' },
          },
          required: ['scriptId'],
        },
      },
      {
        name: 'mcp_list_tools',
        description: 'List tools from one MCP server',
        mode: 'external',
        inputSchema: {
          type: 'object',
          properties: {
            command: { type: 'string' },
            args: { type: 'array', items: { type: 'string' } },
            cwd: { type: 'string' },
          },
          required: ['command'],
        },
      },
      {
        name: 'mcp_tool_exec',
        description: 'Execute one MCP tool',
        mode: 'external',
        inputSchema: {
          type: 'object',
          properties: {
            command: { type: 'string' },
            args: { type: 'array', items: { type: 'string' } },
            cwd: { type: 'string' },
            toolName: { type: 'string' },
            toolArgs: { type: 'object' },
          },
          required: ['command', 'toolName'],
        },
      },
      // deferred 层的发现缝（Wave 2 工具分层）：MCP 动态工具不占每轮
      // 列表，模型用这个工具按相关度找到它们，命中即调。
      {
        name: TOOL_SEARCH_TOOL_NAME,
        description:
          'Search for additional tools not in the initial tool list. Returns full tool schemas; a matched tool can be called immediately by name.',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            query: {
              type: 'string',
              description:
                'Search terms ranked against deferred tool names and descriptions, best matches first.',
            },
            max_results: {
              type: 'integer',
              description: `Cap on returned schemas (default ${TOOL_SEARCH_DEFAULT_MAX_RESULTS}).`,
            },
          },
          required: ['query'],
          additionalProperties: false,
        },
      },
      // ─── 4.6a/4.6b 跨 turn 后台任务 + git worktree 隔离 ─────────────
      // 服务未接线（CLI/test）时一个 schema 都不出——模型看不到不可用的工具。
      ...this.listTaskAndWorktreeSchemas(),
      ...this.listWebToolSchemas(),
      ...this.listRegisteredMcpToolSchemas(),
      ...this.listPluginToolSchemas(),
      // ─── 场景包贡献的工具（0.3a 开槽） ─────────────────
      ...[...this.contributedTools.values()].map((c) => ({
        name: c.name,
        description: c.description,
        inputSchema: c.inputSchema,
        mode: c.mode,
        ...(c.exposure ? { exposure: c.exposure } : {}),
      })),
    ];
  }

  /**
   * 插件生命周期工具（框架 plugin.* RPC 的模型面）。全在 deferred 层：
   * 插件管理是低频运维动作，不值每轮工具列表的 token——模型经
   * tool_search 发现，命中即调。写操作（enable/disable/reload）标
   * destructive，走宿主审批链。
   */
  private listPluginToolSchemas(): ToolSchema[] {
    if (!this.pluginRpc) return [];
    const nameParam = {
      type: 'object',
      properties: {
        name: { type: 'string', description: 'The plugin name from plugin_list.' },
      },
      required: ['name'],
      additionalProperties: false,
    } as const;
    return [
      {
        name: 'plugin_list',
        description:
          'List every tool plugin the agent runtime has loaded, with lifecycle state (enabled, reloadable) and the tools each provides.',
        mode: 'read',
        exposure: 'deferred',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      },
      {
        name: 'plugin_enable',
        description:
          'Enable a disabled plugin so its tools become dispatchable again. Use plugin_list first for the exact name.',
        mode: 'destructive',
        exposure: 'deferred',
        inputSchema: nameParam,
      },
      {
        name: 'plugin_disable',
        description:
          'Disable a plugin without unloading its code; its tools leave the dispatch surface until re-enabled. Use plugin_list first for the exact name.',
        mode: 'destructive',
        exposure: 'deferred',
        inputSchema: nameParam,
      },
      {
        name: 'plugin_reload',
        description:
          'Hot-reload a reloadable plugin from its source, picking up code changes without a restart. Use plugin_list first for the exact name.',
        mode: 'destructive',
        exposure: 'deferred',
        inputSchema: nameParam,
      },
    ];
  }

  /**
   * 4.6a/4.6b 任务与 worktree 工具的 schema。task_run 立即返回
   * （任务在后台独立流里跨 turn 跑完）；worktree_* 操作绑定项目的
   * git 仓库。description 用英文（面向模型），与 local_* 工具同款。
   */
  private listTaskAndWorktreeSchemas(): ToolSchema[] {
    if (!this.taskService || !this.worktreeService) return [];
    return [
      {
        name: 'task_run',
        description:
          'Run a self-contained task in the background as an independent agent run that keeps going after this turn ends. '
          + 'Returns immediately with {taskId, status:"running"}; check progress with task_status and collect the final answer with task_result. '
          + 'No project binding is required — omit worktree for a plain background run. '
          + 'worktree:true is OPTIONAL isolation for code changes: it needs the chat bound to a git project and runs the task '
          + 'in a fresh worktree (its own branch) that the user can merge or discard from the task panel. '
          + 'If a worktree run fails on project binding, retry WITHOUT worktree instead of refusing the task. '
          + 'Orchestration: dependsOn (taskIds of THIS chat) makes the task wait (status:"blocked") until every dependency completes, '
          + 'then it starts automatically — build pipelines like "run tests after the fix task". A failed dependency fails the task fast.',
        mode: 'local',
        inputSchema: {
          type: 'object',
          properties: {
            task: {
              type: 'string',
              description:
                'Complete, self-contained instructions — the background run sees none of this conversation.',
            },
            worktree: {
              type: 'boolean',
              description:
                'Run inside a fresh isolated git worktree (requires the chat to be bound to a project that is a git repo).',
            },
            worktreeName: {
              type: 'string',
              description: 'Optional worktree/branch name slug (default: auto).',
            },
            dependsOn: {
              type: 'array',
              items: { type: 'string' },
              description:
                'Optional taskIds (from earlier task_run results in this chat) that must all complete before this task starts.',
            },
          },
          required: ['task'],
          additionalProperties: false,
        },
      },
      {
        name: 'task_send',
        description:
          'Send a message into a RUNNING background task — the task receives it mid-run as steering (agent-to-agent messaging). '
          + 'Use it to narrow scope, add findings, or redirect a running task. Fails with needsFollowup while the task is blocked or still starting; '
          + 'a finished task cannot receive messages — run a new task instead.',
        mode: 'local',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: { type: 'string' },
            message: {
              type: 'string',
              description: 'The steering message the running task receives.',
            },
          },
          required: ['taskId', 'message'],
          additionalProperties: false,
        },
      },
      {
        name: 'task_status',
        description:
          'Check background task status. With taskId returns that task; without it lists this chat\'s tasks (running first by recency).',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: { type: 'string' },
          },
          additionalProperties: false,
        },
      },
      {
        name: 'task_result',
        description:
          'Collect the final answer of a finished background task. Fails with needsFollowup while the task is still running — poll later instead of re-running the task.',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: { type: 'string' },
          },
          required: ['taskId'],
          additionalProperties: false,
        },
      },
      {
        name: 'worktree_create',
        description:
          'Create an isolated git worktree of the bound project (directory <project>/.steerable/worktrees/<name> on a new steerable/<name> branch). '
          + 'Returns its path. Idempotent on the name — an existing worktree is returned as-is.',
        mode: 'safe_write',
        inputSchema: {
          type: 'object',
          properties: {
            name: { type: 'string', description: 'Worktree name slug (default: auto).' },
          },
          additionalProperties: false,
        },
      },
      {
        name: 'worktree_list',
        description: 'List the bound project\'s managed worktrees (name, path, branch).',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {},
          additionalProperties: false,
        },
      },
      {
        name: 'worktree_remove',
        description:
          'Remove a managed worktree and delete its branch — the DISCARD path, any unmerged changes are lost. '
          + 'To keep the changes, ask the user to merge from the task panel instead.',
        mode: 'destructive',
        inputSchema: {
          type: 'object',
          properties: {
            name: { type: 'string', description: 'Worktree name or path.' },
          },
          required: ['name'],
          additionalProperties: false,
        },
      },
    ];
  }

  /**
   * 网络读取对（W5-2）：schema 与 sidecar `web_tools.py` 保持一致；description
   * 用英文（面向模型），与 local_* 工具同款。可用集由握手决定，未握手/未注册
   * 时一个 schema 都不出——模型看不到不可用的工具。
   */
  private listWebToolSchemas(): ToolSchema[] {
    if (!this.webToolDelegate) return [];
    const schemas: ToolSchema[] = [];
    if (this.webToolNames.has('web_fetch')) {
      schemas.push({
        name: 'web_fetch',
        description:
          'Fetch one public web page over http(s) and return its text (HTML is converted to plain text). '
          + 'Private/loopback/link-local targets are refused; cross-origin redirects are reported, not followed — re-issue the call with the reported URL.',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            url: { type: 'string', description: 'The http(s) URL to fetch.' },
          },
          required: ['url'],
          additionalProperties: false,
        },
      });
    }
    if (this.webToolNames.has('web_search')) {
      schemas.push({
        name: 'web_search',
        description:
          'Search the public web. Returns titled results with URLs and snippets; follow up with web_fetch on a result URL to read the page.',
        mode: 'read',
        inputSchema: {
          type: 'object',
          properties: {
            query: { type: 'string', description: 'The search query.' },
            max_results: {
              type: 'integer',
              description: 'Cap on returned results (default 8, ceiling 20).',
            },
          },
          required: ['query'],
          additionalProperties: false,
        },
      });
    }
    return schemas;
  }

  /**
   * 已注册 MCP 服务的动态工具。服务的工具列表来自注册表的内存缓存
   * （启动后台刷新 + 设置界面"测试连接"刷新），没缓存或已禁用的服务不出场。
   * 全部落在 deferred 层：MCP 目录是最大的无界第三方工具来源，不为每个
   * schema 每轮付 token——模型经 `tool_search` 发现，命中即调。
   */
  private listRegisteredMcpToolSchemas(): ToolSchema[] {
    if (!this.mcpRegistry) return [];
    const out: ToolSchema[] = [];
    for (const server of this.mcpRegistry.list()) {
      if (!server.enabled) continue;
      const cached = this.mcpRegistry.getCachedTools(server.id);
      if (!cached || cached.tools.length === 0) continue;
      const key = this.mcpRegistry.serverKey(server);
      for (const tool of cached.tools) {
        out.push({
          name: `${MCP_DYNAMIC_TOOL_PREFIX}${key}__${tool.name}`,
          description: `[MCP:${server.name}] ${tool.description || tool.name}`,
          inputSchema:
            tool.inputSchema && typeof tool.inputSchema === 'object'
              ? (tool.inputSchema as Record<string, unknown>)
              : { type: 'object', properties: {} },
          mode: 'external',
          exposure: 'deferred',
        });
      }
    }
    return out;
  }

  /**
   * 模型可见列表：仅 direct 层。`tool_search` 只有在存在 deferred 层时
   * 才占一个列表槽——没有可发现的东西时它是每轮请求里的死重。
   * 分发不按层设卡：被发现的 deferred 工具按名直调（execute 不查层）。
   *
   * @param policy 本轮智能体的工具准入策略；被拒的工具连 schema 都不出。
   *   deferred 层同样按策略过滤，所以策略拒掉全部 deferred 工具时
   *   `tool_search` 也一并退场（没有可发现的东西）。缺省 = 不限制。
   */
  listModelSchemas(policy?: AgentToolPolicy | null): ToolSchema[] {
    const effective = policy ?? { mode: 'all', tools: [] };
    const all = filterToolsByPolicy(this.listSchemas(), effective);
    const hasDeferred = all.some(s => s.exposure === 'deferred');
    return all.filter(s => {
      if ((s.exposure ?? 'direct') !== 'direct') return false;
      if (s.name === TOOL_SEARCH_TOOL_NAME && !hasDeferred) return false;
      return true;
    });
  }

  getSchemaByName(toolName: string): ToolSchema | null {
    return this.listSchemas().find(schema => schema.name === toolName) || null;
  }

  async execute(call: ToolCall, context?: ToolExecContext): Promise<unknown> {
    const args = call.arguments || {};
    const projectRoot = context?.projectRoot ?? null;
    // 智能体工具策略的分发层复检：广告层（listModelSchemas / tool_search）
    // 已经把被拒的工具藏了，但模型可能从历史或幻觉里捞出名字直调。
    const toolPolicy = context?.toolPolicy ?? null;
    if (toolPolicy && !isToolAllowed(toolPolicy, call.name)) {
      throw new Error(
        `工具 ${call.name} 被当前智能体的工具策略拒绝（智能体管理 → 工具权限）`,
      );
    }
    if (call.name.startsWith(MCP_DYNAMIC_TOOL_PREFIX)) {
      return await this.executeRegisteredMcpTool(call.name, args);
    }
    switch (call.name) {
      case TOOL_SEARCH_TOOL_NAME:
        return this.executeToolSearch(args, toolPolicy);
      case 'web_fetch':
      case 'web_search':
        return await this.executeWebTool(call.name, args);
      case 'local_exec_shell':
        return await this.executeShell(
          {
            command: String(args.command || ''),
            cwd: typeof args.cwd === 'string' ? args.cwd : undefined,
            timeout: typeof args.timeout === 'number' ? args.timeout : undefined,
          },
          projectRoot,
        );
      case 'local_read_file':
        return await this.localExecutor.readLocalFile(
          {
            path: String(args.path || ''),
          },
          projectRoot,
          context?.additionalReadRoots ?? null,
        );
      case 'local_write_file': {
        const written = await this.localExecutor.writeLocalFile(
          {
            path: String(args.path || ''),
            content: String(args.content || ''),
            createDirs: Boolean(args.createDirs),
            expectedVersion:
              typeof args.expectedVersion === 'string' ? args.expectedVersion : undefined,
          },
          projectRoot,
        );
        this.autoInstallSkillOnSuccess(written, args.path);
        return written;
      }
      case 'local_edit_file': {
        const edits = Array.isArray(args.edits)
          ? args.edits
              .filter(e => e && typeof e === 'object')
              .map(e => ({
                oldText: String((e as Record<string, unknown>).oldText ?? ''),
                newText: String((e as Record<string, unknown>).newText ?? ''),
              }))
          : [];
        const edited = await this.localExecutor.editLocalFile(
          {
            path: String(args.path || ''),
            edits,
            expectedVersion:
              typeof args.expectedVersion === 'string' ? args.expectedVersion : undefined,
          },
          projectRoot,
        );
        this.autoInstallSkillOnSuccess(edited, args.path);
        return edited;
      }
      case 'local_open_path':
        return await this.localExecutor.openLocalTarget({
          target: String(args.target || ''),
        });
      case 'local_run_snippet': {
        // 与 local_exec_shell 同款项目沙箱：cwd 默认项目根，显式越界拒绝。
        const sandboxed = this.applyProjectCwdSandbox(
          { cwd: typeof args.cwd === 'string' ? args.cwd : undefined },
          projectRoot,
        );
        if ('error' in sandboxed) return sandboxed.error;
        return await this.localExecutor.runCode({
          language: String(args.language || ''),
          code: String(args.code || ''),
          cwd: sandboxed.request.cwd,
          timeout: typeof args.timeout === 'number' ? args.timeout : undefined,
          pipPackages: Array.isArray(args.pipPackages)
            ? args.pipPackages.map(String)
            : undefined,
          npmPackages: Array.isArray(args.npmPackages)
            ? args.npmPackages.map(String)
            : undefined,
        });
      }
      case 'local_list_scripts':
        return { success: true, scripts: this.localScriptRegistry.list() };
      case 'local_run_script': {
        const scriptId = String(args.scriptId || '');
        const script = this.localScriptRegistry.getById(scriptId);
        if (!script) {
          return { success: false, error: `Script not found: ${scriptId}` };
        }
        return await this.localExecutor.executeShell({
          command: script.command,
          cwd: script.cwd,
          timeout: script.timeout,
        });
      }
      case 'mcp_list_tools': {
        const cfg = this.resolveMcpConfig(args);
        return await mcpExecutor.listTools(cfg);
      }
      case 'mcp_tool_exec': {
        const cfg = this.resolveMcpConfig(args);
        const toolName = String(args.toolName || '');
        const toolArgs = (args.toolArgs || {}) as Record<string, unknown>;
        return await mcpExecutor.executeTool(cfg, toolName, toolArgs);
      }
      case 'plugin_list':
      case 'plugin_enable':
      case 'plugin_disable':
      case 'plugin_reload': {
        if (!this.pluginRpc) {
          throw new Error('plugin tools unavailable: sidecar plugin registry not wired');
        }
        const method = call.name.replace('_', '.') as
          | 'plugin.list'
          | 'plugin.enable'
          | 'plugin.disable'
          | 'plugin.reload';
        const params = call.name === 'plugin_list' ? undefined : { name: String(args.name || '') };
        return await this.pluginRpc(method, params);
      }

      case 'task_run': {
        const chatId = this.requireTaskContext(context);
        const dependsOn = Array.isArray(args.dependsOn)
          ? args.dependsOn.filter((x): x is string => typeof x === 'string' && x.length > 0)
          : undefined;
        const result = await this.taskService!.runTask({
          chatId,
          task: String(args.task || ''),
          worktree: args.worktree === true,
          worktreeName: typeof args.worktreeName === 'string' ? args.worktreeName : undefined,
          dependsOn,
        });
        return {
          success: true,
          ...result,
          hint: result.status === 'blocked'
            ? '任务已登记为 blocked：依赖任务全部完成后会自动点火，不需要你轮询催促。'
            : '任务已在后台独立运行（不随本轮对话结束而停止）。用 task_status 查进度、task_result 取最终结果；不要在本轮等待它。',
        };
      }
      case 'task_send': {
        const chatId = this.requireTaskContext(context);
        return this.taskService!.sendMessage(
          chatId,
          String(args.taskId || ''),
          String(args.message || ''),
        );
      }
      case 'task_status': {
        const chatId = this.requireTaskContext(context);
        return this.taskService!.status(
          chatId,
          typeof args.taskId === 'string' && args.taskId ? args.taskId : undefined,
        );
      }
      case 'task_result': {
        const chatId = this.requireTaskContext(context);
        return this.taskService!.result(chatId, String(args.taskId || ''));
      }
      case 'worktree_create': {
        const chatId = this.requireTaskContext(context);
        const wt = await this.worktreeService!.createWorktree(
          chatId,
          typeof args.name === 'string' && args.name ? args.name : undefined,
        );
        return { success: true, ...wt };
      }
      case 'worktree_list': {
        const chatId = this.requireTaskContext(context);
        const worktrees = await this.worktreeService!.listWorktrees(chatId);
        return { success: true, total: worktrees.length, worktrees };
      }
      case 'worktree_remove': {
        const chatId = this.requireTaskContext(context);
        const result = await this.worktreeService!.removeWorktree(
          chatId,
          String(args.name || ''),
        );
        return { success: true, ...result };
      }

      default: {
        // 场景包贡献的工具在 default 分发：内置工具优先（注册时已拒重名）。
        const contributed = this.contributedTools.get(call.name);
        if (contributed) {
          return await contributed.handler(args, context ?? {});
        }
        return { success: false, error: `Unknown tool: ${call.name}` };
      }
    }
  }

  /**
   * 任务/worktree 工具的调用上下文：必须有来源 chatId（任务表按 chat 归组、
   * 项目围栏按 chat 解析）。schema 出场 ⇒ 服务已接线，这里只缺 chatId 时
   * 抛错（reverse-tools 总是透传；缺了就是接线 bug，响亮失败）。
   */
  private requireTaskContext(context?: ToolExecContext): string {
    if (!this.taskService || !this.worktreeService) {
      throw new Error('任务/worktree 服务未接线（main.ts setTaskServices）');
    }
    const chatId = context?.chatId;
    if (!chatId) {
      throw new Error('任务/worktree 工具需要 chatId 调用上下文');
    }
    return chatId;
  }

  /**
   * Agent 写出 `…/skills/<name>/SKILL.md` 且该目录还不在已知 skill root 上时，
   * 拷进用户技能目录，Skill 设置 / `/` 菜单不必再手动导入。
   */
  private autoInstallSkillOnSuccess(result: unknown, writtenPath: unknown): void {
    if (!result || typeof result !== 'object' || (result as { success?: boolean }).success !== true) {
      return;
    }
    if (typeof writtenPath !== 'string' || !writtenPath) return;
    try {
      maybeAutoInstallWrittenSkill(writtenPath);
    } catch (err) {
      console.warn('[skill-install] auto-install failed', err);
    }
  }

  /**
   * 项目模式硬沙箱（cwd 维度）：projectRoot 非空时，cwd 默认收到项目根；
   * 模型显式给的 cwd 越界则返回错误结果。相对 cwd 按项目根解析（而不是
   * 进程 cwd），符合"在项目里干活"的直觉。
   *
   * 注意边界：shell 命令文本里内嵌的绝对路径无法可靠解析，围栏只覆盖
   * cwd 与文件读写工具——系统提示里已告知模型只在项目目录内操作。
   *
   * local_exec_shell 与 local_run_snippet 共用本 helper。
   */
  private applyProjectCwdSandbox<T extends { cwd?: string }>(
    request: T,
    projectRoot?: string | null,
  ): { request: T } | { error: LocalExecResult } {
    if (!projectRoot) return { request };
    const root = path.resolve(
      projectRoot.startsWith('~') ? path.join(os.homedir(), projectRoot.slice(1)) : projectRoot,
    );
    if (request.cwd) {
      const expanded = request.cwd.startsWith('~')
        ? path.join(os.homedir(), request.cwd.slice(1))
        : request.cwd;
      const resolvedCwd = path.isAbsolute(expanded)
        ? path.resolve(expanded)
        : path.resolve(root, expanded);
      if (!isPathWithinRoot(resolvedCwd, root)) {
        return {
          error: {
            success: false,
            error: buildProjectRootViolation(resolvedCwd, root),
            platform: process.platform,
          },
        };
      }
      return { request: { ...request, cwd: resolvedCwd } };
    }
    return { request: { ...request, cwd: root } };
  }

  private async executeShell(
    request: LocalExecRequest,
    projectRoot?: string | null,
  ): Promise<LocalExecResult> {
    const sandboxed = this.applyProjectCwdSandbox(request, projectRoot);
    if ('error' in sandboxed) return sandboxed.error;
    if (this.shellExecutor) {
      return this.shellExecutor(sandboxed.request);
    }
    return this.localExecutor.executeShell(sandboxed.request);
  }

  private resolveMcpConfig(args: Record<string, unknown>): McpServerConfig {
    return {
      command: String(args.command || ''),
      args: Array.isArray(args.args) ? args.args.map(item => String(item)) : undefined,
      cwd: typeof args.cwd === 'string' ? args.cwd : undefined,
    };
  }

  /**
   * `tool_search` 处理器：BM25 排序 deferred 名录（name 分词计两次），返回
   * 完整 schema，结果有界（默认 8，封顶 20）。算法、常量、默认上限与返回
   * 文案都与 steerable-framework `tool_search.py` 对齐——同名工具在两侧给
   * 出同样的名次；hidden 层不进搜索。
   *
   * 智能体工具策略在这里同样生效：否则每轮列表藏起来的工具会从这条发现缝
   * 漏回去，「限制」只剩一半。
   */
  private executeToolSearch(
    args: Record<string, unknown>,
    policy: AgentToolPolicy | null,
  ): unknown {
    const cap = resolveMaxResults(args.max_results);
    const deferred = filterToolsByPolicy(
      this.listSchemas().filter(s => s.exposure === 'deferred'),
      policy ?? { mode: 'all', tools: [] },
    );
    const ranked = rankTools(deferred, String(args.query ?? ''));
    const matches = ranked.slice(0, cap).map(schema => ({
      name: schema.name,
      description: schema.description,
      parameters: schema.inputSchema,
    }));
    return {
      matches,
      deferredCount: deferred.length,
      note: matches.length
        ? 'Matched tools are registered; call them by name with the returned schema.'
        : 'No deferred tools matched; try different search terms.',
    };
  }

  /**
   * 前转网络读取调用到 sidecar（W5-2）。走到这里说明 schema 已出场，
   * 即握手时 sidecar 注册了该工具；delegate 缺失/集合漂移是编程错误或
   * sidecar 中途重启，按响亮失败处理（reverse-tools 会把抛错包成
   * ToolResult）。
   */
  private async executeWebTool(
    name: 'web_fetch' | 'web_search',
    args: Record<string, unknown>,
  ): Promise<unknown> {
    if (!this.webToolDelegate || !this.webToolNames.has(name)) {
      throw new Error(
        `工具 ${name} 当前不可用：sidecar 未注册该工具` +
          (name === 'web_search'
            ? '（web_search 需要在设置页选择免费搜索或填写 Tavily 钥，或配置 STEERABLE_WEB_SEARCH_API_KEY / TAVILY_API_KEY；OpenAI 可用聊天凭证走托管搜索）'
            : '') +
          '。',
      );
    }
    if (name === 'web_search' && this.hostedWebSearch) {
      return await this.hostedWebSearch(args);
    }
    return await this.webToolDelegate(name, args);
  }

  /** 执行注册表里的 MCP 动态工具（`mcp__<serverKey>__<toolName>`）。 */
  private async executeRegisteredMcpTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<unknown> {
    if (!this.mcpRegistry) {
      return { success: false, error: 'MCP 服务注册表不可用' };
    }
    const rest = name.slice(MCP_DYNAMIC_TOOL_PREFIX.length);
    const sep = rest.indexOf('__');
    if (sep === -1) {
      return { success: false, error: `无法解析的 MCP 工具名: ${name}` };
    }
    const serverKey = rest.slice(0, sep);
    const toolName = rest.slice(sep + 2);
    const server = this.mcpRegistry
      .list()
      .find((s) => s.enabled && this.mcpRegistry!.serverKey(s) === serverKey);
    if (!server) {
      return {
        success: false,
        error: `MCP 服务「${serverKey}」不存在或已禁用。请在 设置 → MCP 服务 中检查配置。`,
      };
    }
    return await mcpExecutor.executeTool(
      this.mcpRegistry.toExecutorConfig(server),
      toolName,
      args,
    );
  }
}
