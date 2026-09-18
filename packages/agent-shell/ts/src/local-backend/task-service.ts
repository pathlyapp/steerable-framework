/**
 * 4.6a Task（跨 turn 后台任务）+ 4.6c Task×Worktree 组合。
 *
 * 核心难点——跨 turn 生命周期：sidecar 的父 turn 结束时会在 finally 里
 * `orchestration.shutdown()` / `subagent_executor.shutdown()`（池寿命 =
 * 父 turn），所以任务**不能**挂在父 turn 的 AgentPool 里。本模块的做法是
 * 让宿主为每个任务起一条**独立的 sidecar chat 流**（复用
 * `streamCoreLoopTurn`，与父 turn 同一 supervisor 进程但 streamId /
 * durable record / CoreLoop / AgentPool 全部独立）：
 *
 *   - 父 turn 的 `task_run` 工具调用只落任务表 + 点火，立即返回
 *     `{taskId, status: "running"}`——任务生命周期由宿主任务表管理；
 *   - 父 turn 流 teardown 只 shutdown 它自己的池，任务流照常跑到终态；
 *   - 任务流以 `task:<taskId>` 为 chatId/recordId： durable record 独立
 *     （trace.fetch 可查），也不与父 chat 的 activeCoreLoopStreams /
 *     turn_active 标记冲突。
 *
 * 任务的工具调用照常走反向通道回宿主执行；toolContext 里带
 * `chatId: <父 chat>`（项目围栏照旧解析）与可选 `workspaceRoot:
 * <worktree 路径>`（4.6b：reverse-tools 优先采用，把 cwd/文件围栏钉到
 * 隔离工作区而不是主检出）。
 *
 * 深度约束与 delegate_subagent 一致：depth-1——任务回合的工具列表里
 * 没有 `task_run`，任务不能再派生任务（任务里仍可 delegate_subagent）。
 */

import path from 'node:path';
import os from 'node:os';

import { llmService, getSidecarSupervisor } from '../llm/index.js';
import { sidecarWireProvider } from '../storage/llm-settings.js';
import type { TaskRecord } from '../storage/index.js';
import type { ToolRouter } from '../tool-router.js';
import type { SidecarChatStreamRequest } from '../sidecar/index.js';
import {
  buildWorldState,
  streamCoreLoopTurn,
} from './coreloop-stream.js';
import { builtinSubagentParam } from './subagent-profiles.js';
import { buildExecSandbox } from '../sidecar/exec-sandbox.js';
import type { WorktreeService } from './worktree-service.js';
import {
  readSidecarHistoryEntries,
  timelineFromHistoryEntries,
} from './task-process.js';
import {
  appendTimelineDelta,
  syncTimelineTools,
  type PersistedTurnBlock,
} from './turn-timeline.js';

/** 任务表的最小异步读写面（生产注入 scoped store；测试注入内存实现）。 */
export interface TaskStore {
  createTask(input: {
    chatId: string;
    task: string;
    worktreePath?: string | null;
    worktreeBranch?: string | null;
    recordId?: string | null;
    dependsOn?: string[] | null;
    initialStatus?: 'blocked' | 'running';
  }): Promise<TaskRecord>;
  getTask(taskId: string): Promise<TaskRecord | null>;
  listTasks(chatId?: string, limit?: number): Promise<TaskRecord[]>;
  updateTask(
    taskId: string,
    updates: Partial<Pick<TaskRecord, 'status' | 'answer' | 'error' | 'worktreeState' | 'traceId' | 'recordId'>>,
  ): Promise<TaskRecord | null>;
  /** 回写推理时间线；缺省实现可为空（测试假 store 内存覆盖）。 */
  saveTaskProcess?(taskId: string, processJson: string): Promise<void>;
}

export interface TaskServiceDeps {
  store: TaskStore | (() => TaskStore);
  toolRouter: ToolRouter;
  worktreeService: WorktreeService;
  /** chatId → 绑定项目（无项目对话返回 null）。与 router.resolveChatProject 同源。 */
  resolveChatProject: (chatId: string) => Promise<{ name: string; folderPath: string } | null>;
  /** 任务终态 / 推理过程广播（renderer 任务面板与右侧过程栏）。 */
  broadcast?: (
    eventName: 'task-updated' | 'task-process',
    payload: Record<string, unknown>,
  ) => void;
  /** 测试缝：替换真实流驱动（默认 streamCoreLoopTurn）。 */
  runStream?: typeof streamCoreLoopTurn;
  /** 测试缝：sidecar 句柄解析（默认 getSidecarSupervisor）。 */
  getSupervisor?: typeof getSidecarSupervisor;
  /** 测试缝：历史回放（默认读 sidecar sqlite）。 */
  readHistory?: (recordId: string) => unknown[];
}

/** 任务回合的系统提示词：自包含、汇报结果、不假装执行。 */
function buildTaskSystemPrompt(input: {
  task: string;
  projectName: string | null;
  workspaceRoot: string | null;
}): string {
  const lines = [
    '你是一个后台任务执行体。用户给了你一项自包含任务；你看不到主对话的上下文，也不要向用户提问（没有 ask_user）。',
    '工作纪律：',
    '1) 直接动手完成任务，需要读写文件/跑命令就真实调用工具；',
    '2) 回答中的每个事实都必须来自本轮真实工具返回，禁止编造；',
    '3) 完成后用一段简洁中文汇报：做了什么、关键结果、遗留事项。',
  ];
  if (input.projectName && input.workspaceRoot) {
    lines.push(
      '',
      `【工作区】本任务在隔离工作区运行，根目录：${input.workspaceRoot}`,
      '你的文件读写与命令执行都被限制在该目录内；相对路径按该根目录解析。',
    );
  } else if (input.projectName) {
    lines.push(
      '',
      `【项目模式】任务绑定项目「${input.projectName}」，文件与命令操作限制在项目目录内。`,
    );
  }
  return lines.join('\n');
}

/** 进行中任务的进程内状态（任务表是 durable 真相）。 */
interface RunningTask {
  promise: Promise<void>;
  /** sidecar 流建立后由 onStreamId 回填，task_send 用它 steer。 */
  streamId: string | null;
  /** 已产出的推理时间线，过程栏的 live 数据源。 */
  timeline: PersistedTurnBlock[];
  /** 工具调用行（按 call id 就地更新），syncTimelineTools 的输入。 */
  actions: Array<Record<string, unknown>>;
}

export class TaskService {
  private readonly running = new Map<string, RunningTask>();

  constructor(private readonly deps: TaskServiceDeps) {}

  private get store(): TaskStore {
    return typeof this.deps.store === 'function' ? this.deps.store() : this.deps.store;
  }

  /**
   * task_run 工具的实现：落任务表 + 点火独立流，立即返回。
   * 任何同步可知的失败（无 sidecar、worktree 建不起来）都抛给工具层，
   * 让模型本轮就看到错误而不是一个永远 running 的幽灵任务。
   *
   * 编排（dependsOn）：依赖必须指向本 chat 已存在的任务——依赖图因此
   * 天然无环（后来的任务不可能被先建的任务依赖）。依赖全部 completed
   * 立即点火；有 running/blocked 依赖则落 blocked，由依赖终态时的
   * maybeUnblock 调度点火；任一依赖已 failed 直接 fail fast。
   */
  async runTask(input: {
    chatId: string;
    task: string;
    worktree?: boolean;
    worktreeName?: string;
    dependsOn?: string[];
  }): Promise<{ taskId: string; status: 'blocked' | 'running'; worktreePath?: string }> {
    const task = input.task.trim();
    if (!task) throw new Error('task_run: task 不能为空');
    const getSupervisor = this.deps.getSupervisor ?? getSidecarSupervisor;
    if (!getSupervisor()) {
      throw new Error('sidecar 未运行，无法启动后台任务');
    }

    const dependsOn = input.dependsOn?.length ? input.dependsOn : null;
    let blockedBy: string[] = [];
    if (dependsOn) {
      const deps = await Promise.all(dependsOn.map(async (id) => {
        const dep = await this.store.getTask(id);
        if (!dep || dep.chatId !== input.chatId) {
          throw new Error(`task_run: 依赖任务不存在或不属于本会话：${id}`);
        }
        return dep;
      }));
      const failed = deps.filter((d) => d.status === 'failed');
      if (failed.length) {
        throw new Error(
          `task_run: 依赖任务已失败（${failed.map((d) => d.id.slice(0, 8)).join('、')}）——`
          + '先处理依赖的失败（重跑或放弃），再建本任务。',
        );
      }
      blockedBy = deps
        .filter((d) => d.status !== 'completed')
        .map((d) => d.id);
    }

    let worktreePath: string | null = null;
    let worktreeBranch: string | null = null;
    if (input.worktree) {
      try {
        const wt = await this.deps.worktreeService.createWorktree(
          input.chatId,
          input.worktreeName,
        );
        worktreePath = wt.path;
        worktreeBranch = wt.branch;
      } catch (err) {
        // 模型可见的纠错路径：worktree 是可选隔离，失败不等于任务不能跑——
        // 不带 worktree 重试即可（无项目对话完全合法）。
        const msg = err instanceof Error ? err.message : String(err);
        throw new Error(`${msg}（worktree 只是可选隔离；去掉 worktree 参数重试 task_run 即可运行。）`);
      }
    }

    const blocked = blockedBy.length > 0;
    const record = await this.store.createTask({
      chatId: input.chatId,
      task,
      worktreePath,
      worktreeBranch,
      dependsOn,
      initialStatus: blocked ? 'blocked' : 'running',
    });
    const recordId = `task:${record.id}`;
    await this.store.updateTask(record.id, { recordId });
    if (!blocked) {
      this.ignite(record.id, input.chatId, task, recordId, worktreePath);
    }
    this.deps.broadcast?.('task-updated', { chatId: input.chatId, taskId: record.id });
    return {
      taskId: record.id,
      status: blocked ? 'blocked' : 'running',
      ...(worktreePath ? { worktreePath } : {}),
    };
  }

  /**
   * task_send 工具的实现：向运行中的任务流 steer 一条消息（agent 间
   * 消息）。任务已终态时流已关闭，只能让模型重跑——与框架 pool 的
   * resume 不同，任务流是 fire-and-forget 整流。
   */
  async sendMessage(
    chatId: string,
    taskId: string,
    message: string,
  ): Promise<Record<string, unknown>> {
    const text = message.trim();
    if (!text) return { success: false, error: 'task_send: message 不能为空' };
    const task = await this.store.getTask(taskId);
    if (!task || task.chatId !== chatId) {
      return { success: false, error: `任务不存在：${taskId}` };
    }
    if (task.status === 'blocked') {
      return {
        success: false,
        error: '任务还在等依赖就绪（blocked），尚未点火——消息等它 running 后再发。',
        needsFollowup: true,
      };
    }
    if (task.status !== 'running') {
      return {
        success: false,
        error: `任务已终态（${task.status}），流已关闭不能再收消息；要追加工作请重跑 task_run。`,
      };
    }
    const entry = this.running.get(taskId);
    if (!entry?.streamId) {
      return {
        success: false,
        error: '任务流还在建立中，稍后再发。',
        needsFollowup: true,
      };
    }
    const getSupervisor = this.deps.getSupervisor ?? getSidecarSupervisor;
    const supervisor = getSupervisor();
    if (!supervisor) {
      return { success: false, error: 'sidecar 未运行' };
    }
    const ok = await supervisor.steerChat(entry.streamId, text);
    if (!ok) {
      return { success: false, error: 'steer 失败：sidecar 拒绝了消息（流可能刚结束）。' };
    }
    return { success: true, taskId, delivered: true };
  }

  /**
   * 调度器：一个任务到终态后，扫描同 chat 的 blocked 任务——依赖全部
   * completed 的点火；任一依赖 failed 的标 failed（fail fast，错误
   * 指明是哪个依赖挂了）。
   */
  private async maybeUnblock(chatId: string): Promise<void> {
    const blocked = (await this.store
      .listTasks(chatId, 100))
      .filter((t) => t.status === 'blocked' && t.dependsOn?.length);
    for (const task of blocked) {
      const deps = await Promise.all(
        task.dependsOn!.map((id) => this.store.getTask(id)),
      );
      const failedDep = deps.find((d) => d?.status === 'failed');
      if (failedDep) {
        await this.store.updateTask(task.id, {
          status: 'failed',
          error: `依赖任务失败：${failedDep.id.slice(0, 8)}（${failedDep.error ?? '无错误信息'}）`,
        });
        this.deps.broadcast?.('task-updated', { chatId, taskId: task.id });
        continue;
      }
      const allCompleted = deps.every((d) => d?.status === 'completed');
      if (allCompleted) {
        await this.store.updateTask(task.id, { status: 'running' });
        this.ignite(task.id, task.chatId, task.task, task.recordId ?? `task:${task.id}`, task.worktreePath);
        this.deps.broadcast?.('task-updated', { chatId, taskId: task.id });
      }
    }
  }

  /** 点火：独立 sidecar 流跑任务，终态回写任务表。fire-and-forget。 */
  private ignite(
    taskId: string,
    chatId: string,
    task: string,
    recordId: string,
    worktreePath: string | null,
  ): void {
    const entry: RunningTask = {
      promise: Promise.resolve(),
      streamId: null,
      timeline: [],
      actions: [],
    };
    entry.promise = this.executeTask(taskId, chatId, task, recordId, worktreePath, entry)
      .catch((err) => {
        // executeTask 内部已兜底回写；这里是双保险（比如回写本身炸了）。
        console.warn('[task-service] task stream escaped error handling', err);
      })
      .finally(() => {
        this.running.delete(taskId);
        // 一个任务到终态，可能让同 chat 的 blocked 任务依赖就绪——调度点火。
        void this.maybeUnblock(chatId);
      });
    this.running.set(taskId, entry);
  }

  private readonly processFlush = new Map<string, ReturnType<typeof setTimeout>>();

  /**
   * 把时间线推给过程栏并回写任务表。整条时间线每次都要重新序列化，
   * 所以文本/推理增量按 250ms 合并（否则每个 token 都发一份全量，长任务
   * 的流量是 O(n²)）；工具事件与终态立即发，卡片状态不能延迟。
   */
  private publishProcess(
    chatId: string,
    taskId: string,
    entry: { timeline: PersistedTurnBlock[] },
    when: 'coalesced' | 'immediate',
  ): void {
    const emit = () => {
      this.deps.broadcast?.('task-process', {
        chatId,
        taskId,
        timeline: entry.timeline,
        live: true,
      });
      void this.store.saveTaskProcess?.(taskId, JSON.stringify(entry.timeline));
    };
    if (when === 'immediate') {
      this.cancelProcessFlush(taskId);
      emit();
      return;
    }
    if (this.processFlush.has(taskId)) return;
    this.processFlush.set(
      taskId,
      setTimeout(() => {
        this.processFlush.delete(taskId);
        emit();
      }, 250),
    );
  }

  private cancelProcessFlush(taskId: string): void {
    const pending = this.processFlush.get(taskId);
    if (pending) clearTimeout(pending);
    this.processFlush.delete(taskId);
  }

  private async executeTask(
    taskId: string,
    chatId: string,
    task: string,
    recordId: string,
    worktreePath: string | null,
    entry: RunningTask,
  ): Promise<void> {
    const getSupervisor = this.deps.getSupervisor ?? getSidecarSupervisor;
    const supervisor = getSupervisor();
    if (!supervisor) {
      await this.store.updateTask(taskId, {
        status: 'failed',
        error: 'sidecar 未运行',
      });
      this.deps.broadcast?.('task-updated', { chatId, taskId });
      return;
    }

    const settings = llmService.getSettings();
    const project = await this.deps.resolveChatProject(chatId);
    // 4.6b 整合：worktree 任务的围栏根 = worktree 路径（更紧——任务摸不到
    // 主检出）；否则沿用项目根。writableRoots 同理收窄。
    const fenceRoot = worktreePath ?? project?.folderPath ?? null;
    const execSandbox = buildExecSandbox(fenceRoot ? [fenceRoot] : []);
    const approval: SidecarChatStreamRequest['approval'] =
      process.env.STEERABLE_APPROVAL === '0'
        ? undefined
        : {
            mode: 'host',
            timeoutMs: 120_000,
            storePath: path.join(os.homedir(), '.steerable', 'approvals.json'),
          };

    // depth-1：任务回合没有 task_run（不能再派生任务），其余工具面与
    // 普通回合一致；task_status / task_result 保留（任务可以自查/互查）。
    const tools = this.deps.toolRouter
      .listModelSchemas()
      .filter((schema) => schema.name !== 'task_run')
      .map((schema) => ({
        name: schema.name,
        description: schema.description,
        inputSchema: schema.inputSchema,
      }));

    let answer = '';
    let status: TaskRecord['status'] = 'completed';
    let error: string | null = null;
    let traceId: string | undefined;
    try {
      const runStream = this.deps.runStream ?? streamCoreLoopTurn;
      const outcome = await runStream({
        supervisor,
        // 独立身份：record/流/池都不与父 chat 共享（见模块头注释）。
        chatId: recordId,
        recordId,
        systemPrompt: buildTaskSystemPrompt({
          task,
          projectName: project?.name ?? null,
          workspaceRoot: fenceRoot,
        }),
        messages: [{ role: 'user', content: task }],
        tools,
        provider: sidecarWireProvider(settings.provider),
        model: settings.model,
        baseUrl: settings.baseUrl,
        apiKey: settings.apiKey,
        temperature: settings.temperature,
        // 项目围栏按父 chat 解析；worktree 任务再叠加 workspaceRoot 收窄。
        toolContext: {
          mode: 'agent',
          chatId,
          taskId,
          ...(worktreePath ? { workspaceRoot: worktreePath } : {}),
        },
        worldState: buildWorldState({
          mode: 'agent',
          permissions: {
            approval: approval ? 'host' : 'off',
            sandbox: {
              enabled: execSandbox.enabled,
              writableRoots: execSandbox.writableRoots ?? [],
              network: execSandbox.network ?? true,
            },
          },
        }),
        execSandbox,
        approval,
        // 与主对话同一套内置画像（explore/research/coder）——任务回合的
        // 子代理编排语义不分叉。
        subagent: builtinSubagentParam(),
        // 后台任务没有盯着屏幕的用户——不挂 ask_user（问题卡片无人应答），
        // 任务提示词里也声明了"不要提问"。
        askUser: false,
        onStreamId: (streamId) => {
          entry.streamId = streamId;
        },
        onText: (delta) => {
          answer += delta;
          appendTimelineDelta(entry.timeline, 'text', delta);
          this.publishProcess(chatId, taskId, entry, 'coalesced');
        },
        onReasoning: (delta) => {
          appendTimelineDelta(entry.timeline, 'reasoning', delta);
          this.publishProcess(chatId, taskId, entry, 'coalesced');
        },
        onToolStart: (call) => {
          entry.actions.push({
            id: call.id,
            tool: call.tool,
            arguments: call.arguments,
            threw: false,
          });
          syncTimelineTools(entry.timeline, entry.actions);
          this.publishProcess(chatId, taskId, entry, 'immediate');
        },
        onToolAction: (action) => {
          const idx = action.id
            ? entry.actions.findIndex((row) => row.id === action.id)
            : -1;
          const row: Record<string, unknown> = {
            id: action.id,
            tool: action.tool,
            arguments: action.arguments,
            result: action.result,
            success: action.success,
            error: action.error,
            durationMs: action.durationMs,
            ...(action.sandbox ? { sandbox: action.sandbox } : {}),
            threw: false,
          };
          if (idx >= 0) entry.actions[idx] = row;
          else entry.actions.push(row);
          syncTimelineTools(entry.timeline, entry.actions);
          this.publishProcess(chatId, taskId, entry, 'immediate');
        },
      });
      traceId = outcome.traceId;
      if (outcome.status !== 'completed') {
        status = 'failed';
        error = outcome.reason ?? `任务流以 ${outcome.status} 结束`;
      }
    } catch (err) {
      status = 'failed';
      error = err instanceof Error ? err.message : String(err);
      const failureTraceId = (err as { traceId?: unknown })?.traceId;
      if (typeof failureTraceId === 'string' && failureTraceId) {
        traceId = failureTraceId;
      }
    }

    await this.store.updateTask(taskId, {
      status,
      answer: answer.trim() || null,
      error,
      ...(traceId ? { traceId } : {}),
    });
    this.cancelProcessFlush(taskId);
    await this.store.saveTaskProcess?.(taskId, JSON.stringify(entry.timeline));
    this.deps.broadcast?.('task-updated', { chatId, taskId });
    this.deps.broadcast?.('task-process', {
      chatId,
      taskId,
      timeline: entry.timeline,
      live: false,
    });
  }

  /** task_status 工具：单任务或本 chat 全部任务的快照。 */
  async status(chatId: string, taskId?: string): Promise<Record<string, unknown>> {
    if (taskId) {
      const task = await this.store.getTask(taskId);
      if (!task || task.chatId !== chatId) {
        return { success: false, error: `任务不存在：${taskId}` };
      }
      return { success: true, task: this.toModelView(task) };
    }
    const tasks = await this.store.listTasks(chatId, 50);
    return {
      success: true,
      total: tasks.length,
      tasks: tasks.map((t) => this.toModelView(t)),
    };
  }

  /** task_result 工具：取终态任务的完整结果。 */
  async result(chatId: string, taskId: string): Promise<Record<string, unknown>> {
    const task = await this.store.getTask(taskId);
    if (!task || task.chatId !== chatId) {
      return { success: false, error: `任务不存在：${taskId}` };
    }
    if (task.status === 'running') {
      return {
        success: false,
        error: '任务仍在运行，稍后再用 task_result 查询（或先 task_status 看进度）。',
        needsFollowup: true,
        task: this.toModelView(task),
      };
    }
    return {
      success: task.status === 'completed',
      task: this.toModelView(task),
      answer: task.answer,
      error: task.error,
    };
  }

  /** 喂给模型的任务视图（完整字段，answer 截断防爆上下文）。 */
  private toModelView(task: TaskRecord): Record<string, unknown> {
    return {
      taskId: task.id,
      status: task.status,
      task: task.task,
      answerPreview: task.answer ? task.answer.slice(0, 500) : null,
      error: task.error,
      worktree: task.worktreePath
        ? {
            path: task.worktreePath,
            branch: task.worktreeBranch,
            state: task.worktreeState,
          }
        : null,
      createdAt: task.createdAt,
      updatedAt: task.updatedAt,
    };
  }

  /**
   * 右侧过程栏：当前推理时间线。running 且进程内有流 = live；
   * 表上 running 但本进程没流 = 上次崩溃残留（stale）。
   */
  async getProcess(taskId: string): Promise<{
    task: TaskRecord;
    timeline: PersistedTurnBlock[];
    live: boolean;
    stale: boolean;
  } | null> {
    const task = await this.store.getTask(taskId);
    if (!task) return null;
    const entry = this.running.get(taskId);
    if (entry) {
      // 终态回写发生在 running 表项清理之前（清理挂在流 promise 的
      // finally 上），所以 live 认状态而不只认表项在不在。
      return {
        task,
        timeline: entry.timeline,
        live: task.status === 'running',
        stale: false,
      };
    }
    const persisted = parseProcessJson(task.processJson);
    if (persisted) {
      return {
        task,
        timeline: persisted,
        live: false,
        stale: task.status === 'running',
      };
    }
    const recordId = task.recordId ?? `task:${task.id}`;
    const readHistory = this.deps.readHistory ?? readSidecarHistoryEntries;
    const timeline = timelineFromHistoryEntries(readHistory(recordId));
    return {
      task,
      timeline,
      live: false,
      stale: task.status === 'running',
    };
  }

  /** 合并任务 worktree 到主仓（UI「合并到主仓」按钮的路由实现）。 */
  async mergeTaskWorktree(taskId: string): Promise<TaskRecord> {
    const task = await this.store.getTask(taskId);
    if (!task) throw new Error(`任务不存在：${taskId}`);
    if (!task.worktreePath || !task.worktreeBranch) {
      throw new Error('该任务没有关联的 worktree');
    }
    if (task.status === 'running') {
      throw new Error('任务仍在运行，等它结束后再合并');
    }
    if (task.worktreeState !== 'pending') {
      throw new Error(`该任务的 worktree 已处理（${task.worktreeState}）`);
    }
    const name = path.basename(task.worktreePath);
    await this.deps.worktreeService.mergeWorktree(
      task.chatId,
      name,
      `task(${taskId.slice(0, 8)}): ${task.task.slice(0, 60)}`,
    );
    const updated = await this.store.updateTask(taskId, { worktreeState: 'merged' });
    this.deps.broadcast?.('task-updated', { chatId: task.chatId, taskId });
    return updated as TaskRecord;
  }

  /** 丢弃任务 worktree（UI「丢弃」按钮的路由实现）。 */
  async discardTaskWorktree(taskId: string): Promise<TaskRecord> {
    const task = await this.store.getTask(taskId);
    if (!task) throw new Error(`任务不存在：${taskId}`);
    if (!task.worktreePath) {
      throw new Error('该任务没有关联的 worktree');
    }
    if (task.status === 'running') {
      throw new Error('任务仍在运行，等它结束后再丢弃');
    }
    if (task.worktreeState !== 'pending') {
      throw new Error(`该任务的 worktree 已处理（${task.worktreeState}）`);
    }
    const name = path.basename(task.worktreePath);
    await this.deps.worktreeService.removeWorktree(task.chatId, name, {
      deleteBranch: true,
    });
    const updated = await this.store.updateTask(taskId, { worktreeState: 'discarded' });
    this.deps.broadcast?.('task-updated', { chatId: task.chatId, taskId });
    return updated as TaskRecord;
  }
}

function parseProcessJson(raw: string | null | undefined): PersistedTurnBlock[] | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed) || parsed.length === 0) return null;
    return parsed as PersistedTurnBlock[];
  } catch {
    return null;
  }
}
