/**
 * LocalStore 后台任务（tasks）集成测试：真实 SQLite。
 *
 * 覆盖：createTask 的初始态推导（worktreeState / dependsOn / initialStatus）、
 * getTask / listTasks、updateTask 的 undefined 过滤与显式置空、
 * saveTaskProcess 不碰 updated_at 的约定、failRunningTasks 的启动清扫、
 * 以及 depends_on 列坏行的归一（parseDependsOn）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
  vi.useRealTimers();
});

describe('LocalStore / createTask 初始态', () => {
  it('最小输入：running、可空列全 null', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const task = await store.createTask({ chatId: chat.id, task: '统计行数' });
    expect(task).toMatchObject({
      chatId: chat.id,
      task: '统计行数',
      status: 'running',
      answer: null,
      error: null,
      worktreePath: null,
      worktreeBranch: null,
      worktreeState: null,
      recordId: null,
      traceId: null,
      dependsOn: null,
      processJson: null,
    });
    expect(task.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(task.createdAt).toBe(task.updatedAt);
    expect(await store.getTask(task.id)).toEqual(task);
  });

  it('worktree 任务一创建就 pending；非 worktree 任务该列保持 null', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const wt = await store.createTask({
      chatId: chat.id,
      task: '隔离区改代码',
      worktreePath: '/repo/.steerable/worktrees/demo',
      worktreeBranch: 'steerable/demo',
    });
    expect(wt.worktreeState).toBe('pending');
    const plain = await store.createTask({ chatId: chat.id, task: '普通任务' });
    expect(plain.worktreeState).toBeNull();
  });

  it('dependsOn 非空落 JSON 数组，空数组归一为 null；initialStatus 生效', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const dep = await store.createTask({ chatId: chat.id, task: '依赖' });
    const blocked = await store.createTask({
      chatId: chat.id,
      task: '等依赖',
      dependsOn: [dep.id],
      initialStatus: 'blocked',
    });
    expect(blocked.status).toBe('blocked');
    expect(blocked.dependsOn).toEqual([dep.id]);
    const empty = await store.createTask({ chatId: chat.id, task: '空依赖', dependsOn: [] });
    expect(empty.dependsOn).toBeNull();
    expect(empty.status).toBe('running');
  });

  it('getTask 对不存在的 id 返回 null', async () => {
    const { store, db } = await createTestStore();
    expect(await store.getTask('ghost')).toBeNull();
  });
});

describe('LocalStore / listTasks', () => {
  it('按 chat 过滤；缺省 chatId 列全部；updated_at 新→旧', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    const a = await store.createChat('a');
    const b = await store.createChat('b');
    vi.setSystemTime('2026-01-01T00:00:01.000Z');
    const t1 = await store.createTask({ chatId: a.id, task: 't1' });
    vi.setSystemTime('2026-01-01T00:00:02.000Z');
    const t2 = await store.createTask({ chatId: a.id, task: 't2' });
    vi.setSystemTime('2026-01-01T00:00:03.000Z');
    const t3 = await store.createTask({ chatId: b.id, task: 't3' });

    // datetime(updated_at) 截断到秒，逐秒推进保证次序确定。
    expect((await store.listTasks(a.id)).map((t) => t.id)).toEqual([t2.id, t1.id]);
    expect((await store.listTasks()).map((t) => t.id)).toEqual([t3.id, t2.id, t1.id]);
    // limit 截断与 clamp。
    expect(await store.listTasks(undefined, 2)).toHaveLength(2);
    expect(await store.listTasks(undefined, Number.NaN)).toHaveLength(3);
  });
});

describe('LocalStore / updateTask 与 saveTaskProcess', () => {
  it('终态回写：只动传入字段，updated_at 总是刷新', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const task = await store.createTask({ chatId: chat.id, task: 'x', recordId: 'task:1' });
    vi.setSystemTime('2026-01-01T00:00:07.000Z');
    const done = await store.updateTask(task.id, {
      status: 'completed',
      answer: '答案',
      recordId: undefined,
    });
    expect(done).toMatchObject({
      status: 'completed',
      answer: '答案',
      // undefined 不抹掉现有值。
      recordId: 'task:1',
      updatedAt: '2026-01-01T00:00:07.000Z',
    });
  });

  it('显式传 null 置空可空列（与 undefined 的「不动」语义相对）', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const task = await store.createTask({ chatId: chat.id, task: 'x' });
    await store.updateTask(task.id, { status: 'failed', error: '炸了' });
    const cleared = await store.updateTask(task.id, { error: null });
    expect(cleared?.error).toBeNull();
    expect(cleared?.status).toBe('failed');
  });

  it('更新不存在的任务返回 null', async () => {
    const { store, db } = await createTestStore();
    expect(await store.updateTask('ghost', { status: 'completed' })).toBeNull();
  });

  it('saveTaskProcess 写推理时间线但不碰 updated_at', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const task = await store.createTask({ chatId: chat.id, task: 'x' });
    vi.setSystemTime('2026-01-01T00:00:05.000Z');
    await store.saveTaskProcess(task.id, '[{"type":"text"}]');
    const after = await store.getTask(task.id);
    expect(after?.processJson).toBe('[{"type":"text"}]');
    // 流增量不应把任务面板按「最近活动」反复顶到最上。
    expect(after?.updatedAt).toBe('2026-01-01T00:00:00.000Z');
  });
});

describe('LocalStore / failRunningTasks（启动清扫）', () => {
  it('running 与 blocked 落成 failed，completed 不动，返回清扫总数', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const running = await store.createTask({ chatId: chat.id, task: '跑着' });
    const blocked = await store.createTask({ chatId: chat.id, task: '等着', initialStatus: 'blocked' });
    const done = await store.createTask({ chatId: chat.id, task: '完了' });
    await store.updateTask(done.id, { status: 'completed', answer: '好' });

    expect(await store.failRunningTasks('进程已重启')).toBe(2);
    expect(await store.getTask(running.id)).toMatchObject({ status: 'failed', error: '进程已重启' });
    const blockedAfter = await store.getTask(blocked.id);
    expect(blockedAfter?.status).toBe('failed');
    expect(blockedAfter?.error).toContain('进程重启中断了编排等待');
    expect(await store.getTask(done.id)).toMatchObject({ status: 'completed', answer: '好' });
    // 再扫一次没有可清扫的（幂等）。
    expect(await store.failRunningTasks('进程已重启')).toBe(0);
  });
});

describe('LocalStore / depends_on 坏行归一', () => {
  it('非 JSON / 非数组 / 非字符串项都归一为 null 或过滤，不炸读路径', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const task = await store.createTask({ chatId: chat.id, task: 'x', dependsOn: ['a'] });
    const writeDependsOn = (raw: string) =>
      db.prepare(`UPDATE tasks SET depends_on = ? WHERE id = ?`).run(raw, task.id);

    writeDependsOn('not-json');
    expect((await store.getTask(task.id))?.dependsOn).toBeNull();
    writeDependsOn('{"a":1}');
    expect((await store.getTask(task.id))?.dependsOn).toBeNull();
    writeDependsOn('[1, 2]');
    expect((await store.getTask(task.id))?.dependsOn).toBeNull();
    // 空串项被过滤；过滤后为空同样归 null。
    writeDependsOn('["a", ""]');
    expect((await store.getTask(task.id))?.dependsOn).toEqual(['a']);
    writeDependsOn('[""]');
    expect((await store.getTask(task.id))?.dependsOn).toBeNull();
  });
});
