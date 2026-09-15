/**
 * TaskService（4.6a/4.6c）单元测试。
 *
 * 进程外依赖全部走注入缝：TaskStore 用内存假实现，sidecar 流用
 * deps.runStream 桩（记录调用参数、按需回放终态），worktree 用假服务。
 * 被测的是任务生命周期编排本身：落表 → 点火独立流 → 终态回写 → 广播，
 * 以及 Task×Worktree 的围栏收窄与合并/丢弃守卫。
 */
import { randomUUID } from 'node:crypto';

import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { TaskRecord } from '../../src/storage/index.js';
import type { WorktreeService } from '../../src/local-backend/worktree-service.js';
import type { CoreLoopTurnOutcome } from '../../src/local-backend/coreloop-stream.js';
import type { StreamCoreLoopTurnOptions } from '../../src/local-backend/coreloop-stream.js';

// task-service 的模块级依赖在这里全部替掉：llm 设置、sidecar 句柄、
// egress 代理、coreloop-stream（buildWorldState）。
vi.mock('../../src/llm/index.js', () => ({
  llmService: {
    getSettings: () => ({
      provider: 'openai-compat',
      model: 'unit-test-model',
      baseUrl: 'http://127.0.0.1:9/v1',
      apiKey: 'not-a-key',
      temperature: 0,
    }),
  },
  getSidecarSupervisor: () => null,
}));
vi.mock('../../src/sidecar/egress-proxy.js', () => ({
  getActiveEgressProxyEndpoint: () => null,
}));
vi.mock('../../src/local-backend/coreloop-stream.js', () => ({
  buildWorldState: () => ({ mocked: true }),
  streamCoreLoopTurn: async () => ({ status: 'completed' }),
}));

import { TaskService, type TaskStore } from '../../src/local-backend/task-service.js';

/** 内存 TaskStore——语义镜像 SQL 实现（running 初始态、undefined 过滤）。 */
class FakeTaskStore implements TaskStore {
  readonly rows = new Map<string, TaskRecord>();

  createTask(input: {
    chatId: string;
    task: string;
    worktreePath?: string | null;
    worktreeBranch?: string | null;
    recordId?: string | null;
    dependsOn?: string[] | null;
    initialStatus?: 'blocked' | 'running';
  }): TaskRecord {
    const now = new Date().toISOString();
    const record: TaskRecord = {
      id: randomUUID(),
      chatId: input.chatId,
      task: input.task,
      status: input.initialStatus ?? 'running',
      answer: null,
      error: null,
      worktreePath: input.worktreePath ?? null,
      worktreeBranch: input.worktreeBranch ?? null,
      worktreeState: input.worktreePath ? 'pending' : null,
      recordId: input.recordId ?? null,
      traceId: null,
      dependsOn: input.dependsOn?.length ? [...input.dependsOn] : null,
      processJson: null,
      createdAt: now,
      updatedAt: now,
    };
    this.rows.set(record.id, record);
    return record;
  }

  getTask(taskId: string): TaskRecord | null {
    return this.rows.get(taskId) ?? null;
  }

  listTasks(chatId?: string): TaskRecord[] {
    return [...this.rows.values()]
      .filter((t) => !chatId || t.chatId === chatId)
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }

  updateTask(
    taskId: string,
    updates: Partial<
      Pick<TaskRecord, 'status' | 'answer' | 'error' | 'worktreeState' | 'traceId' | 'recordId'>
    >,
  ): TaskRecord | null {
    const existing = this.rows.get(taskId);
    if (!existing) return null;
    const clean = Object.fromEntries(
      Object.entries(updates).filter(([, v]) => v !== undefined),
    );
    const next = { ...existing, ...clean, updatedAt: new Date().toISOString() };
    this.rows.set(taskId, next);
    return next;
  }

  saveTaskProcess(taskId: string, processJson: string): void {
    const existing = this.rows.get(taskId);
    if (!existing) return;
    this.rows.set(taskId, { ...existing, processJson });
  }
}

function makeWorktreeService() {
  return {
    createWorktree: vi.fn(async (_chatId: string, name?: string) => ({
      name: name ?? 'wt-auto',
      path: '/repo/.steerable/worktrees/demo',
      branch: 'steerable/demo',
    })),
    listWorktrees: vi.fn(async () => []),
    removeWorktree: vi.fn(async () => ({ removed: '/repo/.steerable/worktrees/demo', branchDeleted: 'steerable/demo' })),
    mergeWorktree: vi.fn(async () => ({ merged: 'steerable/demo', commit: 'abc123' })),
  } as unknown as WorktreeService & {
    createWorktree: ReturnType<typeof vi.fn>;
    removeWorktree: ReturnType<typeof vi.fn>;
    mergeWorktree: ReturnType<typeof vi.fn>;
  };
}

interface Harness {
  service: TaskService;
  store: FakeTaskStore;
  worktree: ReturnType<typeof makeWorktreeService>;
  broadcasts: Array<{ eventName: string; payload: unknown }>;
  /** 最近一次任务流调用的参数（runStream 桩捕获）。 */
  streamCalls: StreamCoreLoopTurnOptions[];
  /** 回放任务流终态；默认 completed + 经 onText 吐出答案。 */
  setStreamOutcome: (outcome: CoreLoopTurnOutcome & { answer?: string }) => void;
}

function makeService(): Harness {
  const store = new FakeTaskStore();
  const worktree = makeWorktreeService();
  const broadcasts: Array<{ eventName: string; payload: unknown }> = [];
  const streamCalls: StreamCoreLoopTurnOptions[] = [];
  let outcome: CoreLoopTurnOutcome & { answer?: string } = {
    status: 'completed',
    traceId: 'trace-1',
    answer: '任务答案',
  };
  const toolRouter = {
    listModelSchemas: () => [
      { name: 'task_run', description: '', inputSchema: {} },
      { name: 'task_status', description: '', inputSchema: {} },
      { name: 'local_exec_shell', description: '', inputSchema: {} },
    ],
  };
  const service = new TaskService({
    store,
    worktreeService: worktree,
    toolRouter: toolRouter as never,
    resolveChatProject: (chatId) =>
      chatId === 'chat-1' ? { name: 'proj', folderPath: '/repo' } : null,
    broadcast: (eventName, payload) => broadcasts.push({ eventName, payload }),
    getSupervisor: () => ({ fake: true }) as never,
    runStream: async (options) => {
      streamCalls.push(options);
      if (outcome.answer) options.onText?.(outcome.answer);
      const { answer: _answer, ...rest } = outcome;
      return rest;
    },
  });
  return {
    service,
    store,
    worktree,
    broadcasts,
    streamCalls,
    setStreamOutcome: (next) => {
      outcome = next;
    },
  };
}

/** 等任务流跑到终态（ignite 是 fire-and-forget，测试里轮询任务表）。 */
async function waitTaskTerminal(store: FakeTaskStore, taskId: string): Promise<TaskRecord> {
  const deadline = Date.now() + 5_000;
  for (;;) {
    const task = store.getTask(taskId);
    if (task && task.status !== 'running') return task;
    if (Date.now() > deadline) throw new Error('task did not reach terminal state');
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe('task-service / runTask', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeService();
  });

  it('立即返回 running，落任务表，独立流带独立身份', async () => {
    // 流启动那一刻任务行必须是 running（runTask 返回后 stub 流可能已
    // 完成，事后读行会撞上终态——在流内采样才是确定性的）。
    let statusAtStreamStart: string | null = null;
    const sampling = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: {
        listModelSchemas: () => [
          { name: 'task_run', description: '', inputSchema: {} },
          { name: 'local_exec_shell', description: '', inputSchema: {} },
        ],
      } as never,
      resolveChatProject: (chatId) =>
        chatId === 'chat-1' ? { name: 'proj', folderPath: '/repo' } : null,
      broadcast: (eventName, payload) => h.broadcasts.push({ eventName, payload }),
      getSupervisor: () => ({}) as never,
      runStream: async (options) => {
        h.streamCalls.push(options);
        const taskId = String((options.toolContext as { taskId?: unknown })?.taskId);
        statusAtStreamStart = h.store.getTask(taskId)?.status ?? null;
        options.onText?.('任务答案');
        return { status: 'completed', traceId: 'trace-1' };
      },
    });

    const result = await sampling.runTask({ chatId: 'chat-1', task: '统计行数' });
    expect(result.status).toBe('running');
    expect(result.worktreePath).toBeUndefined();
    expect(statusAtStreamStart).toBe('running');

    const row = h.store.getTask(result.taskId);
    expect(row).toMatchObject({
      chatId: 'chat-1',
      task: '统计行数',
      recordId: `task:${result.taskId}`,
    });

    const terminal = await waitTaskTerminal(h.store, result.taskId);
    expect(terminal.status).toBe('completed');
    expect(terminal.answer).toBe('任务答案');
    expect(terminal.traceId).toBe('trace-1');

    // 独立身份：任务流的 chatId/recordId 是 task:<id>，不是父 chat。
    const call = h.streamCalls[0];
    expect(call.chatId).toBe(`task:${result.taskId}`);
    expect(call.recordId).toBe(`task:${result.taskId}`);
    // 工具上下文带父 chatId（项目围栏照旧解析）。
    expect(call.toolContext).toMatchObject({ chatId: 'chat-1', taskId: result.taskId });
    // 后台任务不挂 ask_user。
    expect(call.askUser).toBe(false);
    // 创建与终态各广播一次 task-updated；推理增量另走 task-process。
    expect(
      h.broadcasts.filter((b) => b.eventName === 'task-updated'),
    ).toEqual([
      { eventName: 'task-updated', payload: { chatId: 'chat-1', taskId: result.taskId } },
      { eventName: 'task-updated', payload: { chatId: 'chat-1', taskId: result.taskId } },
    ]);
  });

  it('getProcess 回放流中的推理时间线', async () => {
    const sampling = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: {
        listModelSchemas: () => [{ name: 'local_exec_shell', description: '', inputSchema: {} }],
      } as never,
      resolveChatProject: () => null,
      broadcast: (eventName, payload) => h.broadcasts.push({ eventName, payload }),
      getSupervisor: () => ({}) as never,
      runStream: async (options) => {
        options.onReasoning?.('先跑命令。');
        options.onToolStart?.({
          id: 'c1',
          tool: 'local_exec_shell',
          arguments: { command: 'echo hi' },
        });
        options.onToolAction?.({
          id: 'c1',
          tool: 'local_exec_shell',
          arguments: { command: 'echo hi' },
          success: true,
          result: 'hi',
        });
        options.onText?.('问好完成。');
        return { status: 'completed', traceId: 'trace-p' };
      },
    });
    const result = await sampling.runTask({ chatId: 'chat-1', task: '问好' });
    const terminal = await waitTaskTerminal(h.store, result.taskId);
    expect(terminal.status).toBe('completed');
    const snapshot = sampling.getProcess(result.taskId);
    expect(snapshot?.live).toBe(false);
    expect(snapshot?.stale).toBe(false);
    expect(snapshot?.timeline.map((b) => b.type)).toEqual(['reasoning', 'tools', 'text']);
    expect(h.store.getTask(result.taskId)?.processJson).toContain('先跑命令');
  });

  it('depth-1：任务回合的工具列表没有 task_run', async () => {
    const result = await h.service.runTask({ chatId: 'chat-1', task: 'x' });
    await waitTaskTerminal(h.store, result.taskId);
    const names = h.streamCalls[0].tools.map((t) => t.name);
    expect(names).not.toContain('task_run');
    expect(names).toContain('task_status');
    expect(names).toContain('local_exec_shell');
  });

  it('空任务与无 sidecar 都同步抛错（不留幽灵 running 行）', async () => {
    await expect(h.service.runTask({ chatId: 'chat-1', task: '  ' })).rejects.toThrow('不能为空');
    const noSidecar = new TaskService({
      store: new FakeTaskStore(),
      worktreeService: makeWorktreeService(),
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      getSupervisor: () => null,
    });
    await expect(noSidecar.runTask({ chatId: 'c', task: 'x' })).rejects.toThrow('sidecar 未运行');
  });

  it('流以非 completed 结束 → failed + reason', async () => {
    h.setStreamOutcome({ status: 'cancelled', reason: 'stream cancelled' });
    const result = await h.service.runTask({ chatId: 'chat-1', task: 'x' });
    const terminal = await waitTaskTerminal(h.store, result.taskId);
    expect(terminal.status).toBe('failed');
    expect(terminal.error).toBe('stream cancelled');
  });

  it('流抛异常 → failed + 异常消息', async () => {
    h.setStreamOutcome({ status: 'completed' });
    // 让桩直接抛。
    const throwing = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      getSupervisor: () => ({}) as never,
      runStream: async () => {
        throw new Error('sidecar 连接断开');
      },
    });
    const result = await throwing.runTask({ chatId: 'chat-1', task: 'x' });
    const terminal = await waitTaskTerminal(h.store, result.taskId);
    expect(terminal.status).toBe('failed');
    expect(terminal.error).toBe('sidecar 连接断开');
  });
});

describe('task-service / Task×Worktree', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeService();
  });

  it('worktree:true 先建隔离区，围栏收窄到 worktree，任务行带 worktree 字段', async () => {
    const result = await h.service.runTask({
      chatId: 'chat-1',
      task: '在隔离区改代码',
      worktree: true,
      worktreeName: 'demo',
    });
    expect(result.worktreePath).toBe('/repo/.steerable/worktrees/demo');
    expect(h.worktree.createWorktree).toHaveBeenCalledWith('chat-1', 'demo');

    const row = h.store.getTask(result.taskId);
    expect(row).toMatchObject({
      worktreePath: '/repo/.steerable/worktrees/demo',
      worktreeBranch: 'steerable/demo',
      worktreeState: 'pending',
    });

    const terminal = await waitTaskTerminal(h.store, result.taskId);
    expect(terminal.status).toBe('completed');

    const call = h.streamCalls[0];
    // workspaceRoot 收窄：reverse-tools 会优先用它做围栏根。
    expect(call.toolContext).toMatchObject({
      workspaceRoot: '/repo/.steerable/worktrees/demo',
    });
    // writableRoots 收窄到 worktree（不是项目根）。
    expect(call.execSandbox?.writableRoots).toEqual(['/repo/.steerable/worktrees/demo']);
  });

  it('worktree 建不起来 → 同步抛错，不落任务行', async () => {
    h.worktree.createWorktree.mockRejectedValue(new Error('项目目录不是 git 仓库'));
    await expect(
      h.service.runTask({ chatId: 'chat-1', task: 'x', worktree: true }),
    ).rejects.toThrow('不是 git 仓库');
    expect(h.store.listTasks()).toEqual([]);
  });

  it('mergeTaskWorktree：终态 pending → merged，worktree 被清理', async () => {
    const result = await h.service.runTask({ chatId: 'chat-1', task: 'x', worktree: true, worktreeName: 'demo' });
    await waitTaskTerminal(h.store, result.taskId);

    const merged = await h.service.mergeTaskWorktree(result.taskId);
    expect(merged.worktreeState).toBe('merged');
    expect(h.worktree.mergeWorktree).toHaveBeenCalledWith(
      'chat-1',
      'demo',
      expect.stringContaining('task('),
    );
    // 广播让面板刷新。
    expect(h.broadcasts.at(-1)?.payload).toMatchObject({ taskId: result.taskId });
  });

  it('discardTaskWorktree：终态 pending → discarded + removeWorktree', async () => {
    const result = await h.service.runTask({ chatId: 'chat-1', task: 'x', worktree: true, worktreeName: 'demo' });
    await waitTaskTerminal(h.store, result.taskId);

    const discarded = await h.service.discardTaskWorktree(result.taskId);
    expect(discarded.worktreeState).toBe('discarded');
    expect(h.worktree.removeWorktree).toHaveBeenCalledWith('chat-1', 'demo', { deleteBranch: true });
  });

  it('合并/丢弃守卫：running、无 worktree、重复处理都拒绝', async () => {
    // running 中不可合并——造一个永不结束的任务流。
    const hanging = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      getSupervisor: () => ({}) as never,
      runStream: () => new Promise(() => {}),
    });
    const running = await hanging.runTask({ chatId: 'chat-1', task: 'x', worktree: true, worktreeName: 'demo' });
    await expect(h.service.mergeTaskWorktree(running.taskId)).rejects.toThrow('仍在运行');
    await expect(h.service.discardTaskWorktree(running.taskId)).rejects.toThrow('仍在运行');

    // 非 worktree 任务没有可合并的东西。
    const plain = await h.service.runTask({ chatId: 'chat-1', task: 'y' });
    await waitTaskTerminal(h.store, plain.taskId);
    await expect(h.service.mergeTaskWorktree(plain.taskId)).rejects.toThrow('没有关联的 worktree');

    // 重复处理拒绝（幂等目标态）。
    const wt = await h.service.runTask({ chatId: 'chat-1', task: 'z', worktree: true, worktreeName: 'demo' });
    await waitTaskTerminal(h.store, wt.taskId);
    await h.service.discardTaskWorktree(wt.taskId);
    await expect(h.service.mergeTaskWorktree(wt.taskId)).rejects.toThrow('已处理');
  });
});

describe('task-service / status & result（模型面）', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeService();
  });

  it('status 列表与单查；跨 chat 的任务不可见', async () => {
    const a = await h.service.runTask({ chatId: 'chat-1', task: '任务甲' });
    await waitTaskTerminal(h.store, a.taskId);

    const list = h.service.status('chat-1') as { success: boolean; total: number; tasks: unknown[] };
    expect(list.success).toBe(true);
    expect(list.total).toBe(1);

    const single = h.service.status('chat-1', a.taskId) as { success: boolean; task: { taskId: string } };
    expect(single.task.taskId).toBe(a.taskId);

    // 别的 chat 查这个 id → 不存在（任务表按 chat 归组）。
    const alien = h.service.status('chat-2', a.taskId) as { success: boolean };
    expect(alien.success).toBe(false);
  });

  it('result：running 给 needsFollowup，completed 给完整答案', async () => {
    const hanging = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      getSupervisor: () => ({}) as never,
      runStream: () => new Promise(() => {}),
    });
    const running = await hanging.runTask({ chatId: 'chat-1', task: '慢任务' });
    const pending = hanging.result('chat-1', running.taskId) as {
      success: boolean;
      needsFollowup?: boolean;
    };
    expect(pending.success).toBe(false);
    expect(pending.needsFollowup).toBe(true);

    const done = await h.service.runTask({ chatId: 'chat-1', task: '快任务' });
    await waitTaskTerminal(h.store, done.taskId);
    const result = h.service.result('chat-1', done.taskId) as {
      success: boolean;
      answer: string;
    };
    expect(result.success).toBe(true);
    expect(result.answer).toBe('任务答案');
  });
});

describe('task-service / 编排（dependsOn 调度 + task_send 消息）', () => {
  let h: Harness;
  beforeEach(() => {
    h = makeService();
  });

  /** 等调度器把任务移出 blocked（maybeUnblock 在依赖终态的 finally 里异步跑）。 */
  async function waitTaskNotBlocked(taskId: string): Promise<void> {
    const deadline = Date.now() + 5_000;
    for (;;) {
      const task = h.store.getTask(taskId);
      if (task && task.status !== 'blocked') return;
      if (Date.now() > deadline) throw new Error('task stayed blocked');
      await new Promise((r) => setTimeout(r, 5));
    }
  }

  /** 流挂起直到测试手动释放——依赖编排的断言需要「依赖还在跑」的窗口。 */
  function makeGatedService() {
    const gates: Array<{
      options: StreamCoreLoopTurnOptions;
      release: (outcome: CoreLoopTurnOutcome) => void;
    }> = [];
    const service = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      broadcast: (eventName, payload) => h.broadcasts.push({ eventName, payload }),
      getSupervisor: () => ({}) as never,
      runStream: (options) =>
        new Promise<CoreLoopTurnOutcome>((resolve) => {
          gates.push({ options, release: resolve });
        }),
    });
    return { service, gates };
  }

  it('dependsOn 未就绪 → blocked 落表不点火；依赖完成后自动点火', async () => {
    const g = makeGatedService();
    const a = await g.service.runTask({ chatId: 'chat-1', task: '任务A' });
    expect(a.status).toBe('running');
    expect(g.gates).toHaveLength(1);

    const b = await g.service.runTask({
      chatId: 'chat-1',
      task: '任务B',
      dependsOn: [a.taskId],
    });
    expect(b.status).toBe('blocked');
    // blocked 不点火——仍只有 A 的一条流。
    expect(g.gates).toHaveLength(1);
    const blockedRow = h.store.getTask(b.taskId);
    expect(blockedRow?.status).toBe('blocked');
    expect(blockedRow?.dependsOn).toEqual([a.taskId]);

    // A 到终态 → finally 里的 maybeUnblock 调度点火 B。
    g.gates[0].release({ status: 'completed', traceId: 't-a' });
    await waitTaskNotBlocked(b.taskId);
    expect(g.gates).toHaveLength(2);
    // B 的流用同一任务身份（recordId 不变）。
    expect(g.gates[1].options.recordId).toBe(`task:${b.taskId}`);
    g.gates[1].release({ status: 'completed', traceId: 't-b' });
    const bTerminal = await waitTaskTerminal(h.store, b.taskId);
    expect(bTerminal.status).toBe('completed');
  });

  it('依赖 failed → 阻塞任务 fail fast，错误指明依赖', async () => {
    const g = makeGatedService();
    const a = await g.service.runTask({ chatId: 'chat-1', task: '任务A' });
    const b = await g.service.runTask({
      chatId: 'chat-1',
      task: '任务B',
      dependsOn: [a.taskId],
    });
    expect(b.status).toBe('blocked');

    g.gates[0].release({ status: 'failed', reason: '炸了', traceId: 't-a' });
    await waitTaskNotBlocked(b.taskId);
    const row = h.store.getTask(b.taskId);
    expect(row?.status).toBe('failed');
    expect(row?.error).toContain(a.taskId.slice(0, 8));
    // 依赖挂了不点火。
    expect(g.gates).toHaveLength(1);
  });

  it('依赖不存在 / 已失败 → runTask 直接报错（模型可见）', async () => {
    await expect(
      h.service.runTask({ chatId: 'chat-1', task: 'X', dependsOn: ['ghost'] }),
    ).rejects.toThrow('依赖任务不存在');

    h.setStreamOutcome({ status: 'failed', reason: '炸了', traceId: 't-1' });
    const a = await h.service.runTask({ chatId: 'chat-1', task: '任务A' });
    await waitTaskTerminal(h.store, a.taskId);
    await expect(
      h.service.runTask({ chatId: 'chat-1', task: 'X', dependsOn: [a.taskId] }),
    ).rejects.toThrow('依赖任务已失败');
  });

  it('task_send：运行中任务被 steer；blocked/终态拒绝', async () => {
    const steerCalls: Array<{ streamId: string; content: string }> = [];
    const steering = new TaskService({
      store: h.store,
      worktreeService: h.worktree,
      toolRouter: { listModelSchemas: () => [] } as never,
      resolveChatProject: () => null,
      getSupervisor: () =>
        ({
          steerChat: async (streamId: string, content: string) => {
            steerCalls.push({ streamId, content });
            return true;
          },
        }) as never,
      runStream: async (options) => {
        // 流建立时回填 streamId，然后挂起（任务保持 running）。
        options.onStreamId?.('stream-42');
        await new Promise(() => {});
      },
    });

    const a = await steering.runTask({ chatId: 'chat-1', task: '任务A' });
    await new Promise((r) => setTimeout(r, 10));
    const sent = await steering.sendMessage('chat-1', a.taskId, '缩小范围到 src/');
    expect(sent.success).toBe(true);
    expect(steerCalls).toEqual([{ streamId: 'stream-42', content: '缩小范围到 src/' }]);

    // blocked 任务拒绝（流未点火）。
    const b = await steering.runTask({
      chatId: 'chat-1',
      task: '任务B',
      dependsOn: [a.taskId],
    });
    const blockedSend = await steering.sendMessage('chat-1', b.taskId, 'hi');
    expect(blockedSend.success).toBe(false);
    expect(String(blockedSend.error)).toContain('blocked');

    // 不存在的任务拒绝。
    const ghost = await steering.sendMessage('chat-1', 'ghost', 'hi');
    expect(ghost.success).toBe(false);
  });
});
