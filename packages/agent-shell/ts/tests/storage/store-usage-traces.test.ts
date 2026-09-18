/**
 * LocalStore 用量事件（usage_events）与 harness trace 集成测试。
 *
 * 覆盖：recordUsageEvent 的 token 归一（负数夹 0、小数取整）、
 * getUsageSummary 的分桶聚合 / null 成本桶 / 时间窗口 / 排序，
 * 以及 saveTrace / getTrace / listTracesByChat 的往返与隔离。
 */
import { afterEach, describe, expect, it } from 'vitest';

import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
});

describe('LocalStore / 用量事件聚合', () => {
  it('空表返回全零总计与空分桶', async () => {
    const { store, db } = await createTestStore();
    expect(await store.getUsageSummary()).toEqual({
      sinceDays: 30,
      byModel: [],
      totals: {
        turns: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        cachedPromptTokens: 0,
        costUsd: 0,
      },
    });
  });

  it('同 model+provider 聚合成一桶，按 totalTokens 降序排列', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    await store.recordUsageEvent({
      chatId: chat.id,
      kind: 'chat',
      provider: 'deepseek',
      model: 'deepseek-chat',
      promptTokens: 10,
      completionTokens: 20,
      totalTokens: 30,
      costUsd: 0.001,
    });
    await store.recordUsageEvent({
      chatId: chat.id,
      kind: 'chat',
      provider: 'deepseek',
      model: 'deepseek-chat',
      promptTokens: 1,
      completionTokens: 2,
      totalTokens: 3,
      cachedPromptTokens: 5,
      costUsd: 0.002,
    });
    await store.recordUsageEvent({
      kind: 'title',
      provider: 'ollama',
      model: 'local-model',
      promptTokens: 100,
      completionTokens: 0,
      totalTokens: 100,
    });

    const summary = await store.getUsageSummary(7);
    expect(summary.sinceDays).toBe(7);
    // total_tokens 100 的 local-model 桶排前。
    expect(summary.byModel.map((b) => b.model)).toEqual(['local-model', 'deepseek-chat']);
    const ds = summary.byModel[1];
    expect(ds).toMatchObject({
      provider: 'deepseek',
      turns: 2,
      promptTokens: 11,
      completionTokens: 22,
      totalTokens: 33,
      cachedPromptTokens: 5,
    });
    expect(ds.costUsd).toBeCloseTo(0.003);
    // 无单价桶：costUsd 为 null（面板渲染 "—"），token 照常计入。
    expect(summary.byModel[0].costUsd).toBeNull();
    // 总计：token 全计，成本只累加有单价的桶。
    expect(summary.totals).toMatchObject({
      turns: 3,
      promptTokens: 111,
      completionTokens: 22,
      totalTokens: 133,
      cachedPromptTokens: 5,
    });
    expect(summary.totals.costUsd).toBeCloseTo(0.003);
  });

  it('model 缺省归到 (unknown) 桶；负 token 夹 0、小数取整', async () => {
    const { store, db } = await createTestStore();
    await store.recordUsageEvent({
      kind: 'chat',
      promptTokens: -5,
      completionTokens: 2.9,
      totalTokens: 3.5,
    });
    const summary = await store.getUsageSummary();
    expect(summary.byModel).toHaveLength(1);
    expect(summary.byModel[0]).toMatchObject({
      model: '(unknown)',
      provider: '',
      promptTokens: 0,
      completionTokens: 2,
      totalTokens: 3,
      cachedPromptTokens: 0,
      costUsd: null,
    });
  });

  it('sinceDays 窗口之外的事件不计入', async () => {
    const { store, db } = await createTestStore();
    await store.recordUsageEvent({
      kind: 'chat',
      model: 'm',
      promptTokens: 1,
      completionTokens: 1,
      totalTokens: 2,
    });
    // 把这条记录直接改到 40 天前（recordUsageEvent 总是写当前时间，
    // 窗口行为只能经底层列控制）。
    db
      .prepare(`UPDATE usage_events SET created_at = ?`)
      .run(new Date(Date.now() - 40 * 24 * 60 * 60 * 1000).toISOString());

    expect((await store.getUsageSummary(30)).byModel).toEqual([]);
    expect((await store.getUsageSummary(60)).byModel).toHaveLength(1);
  });
});

describe('LocalStore / harness traces', () => {
  it('saveTrace 往返：可空字段缺省 null，payload 以 JSON 字符串读回', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const trace = await store.saveTrace({
      id: 'trace-1',
      chatId: chat.id,
      startedAtMs: 1000,
      status: 'completed',
      payload: { turns: 2, tools: ['local_exec_shell'] },
    });
    expect(trace).toMatchObject({
      id: 'trace-1',
      chatId: chat.id,
      messageId: null,
      startedAtMs: 1000,
      durationMs: null,
      status: 'completed',
    });
    // HarnessTraceRecord.payload 是存储态的 JSON 字符串，不是解析后的对象。
    expect(JSON.parse(trace.payload)).toEqual({ turns: 2, tools: ['local_exec_shell'] });
    expect(await store.getTrace('trace-1')).toEqual(trace);
  });

  it('listTracesByChat 按 started_at_ms 新→旧，按 chat 隔离，limit clamp', async () => {
    const { store, db } = await createTestStore();
    const a = await store.createChat('a');
    const b = await store.createChat('b');
    for (let i = 1; i <= 3; i += 1) {
      await store.saveTrace({
        id: `t${i}`,
        chatId: a.id,
        messageId: `m${i}`,
        startedAtMs: i * 100,
        durationMs: 10,
        status: 'completed',
        payload: {},
      });
    }
    await store.saveTrace({ id: 'tb', chatId: b.id, startedAtMs: 999, status: 'failed', payload: {} });

    const listed = await store.listTracesByChat(a.id);
    expect(listed.map((t) => t.id)).toEqual(['t3', 't2', 't1']);
    expect(listed[0].messageId).toBe('m3');
    expect(listed[0].durationMs).toBe(10);
    expect((await store.listTracesByChat(a.id, 2)).map((t) => t.id)).toEqual(['t3', 't2']);
    expect(await store.listTracesByChat(a.id, Number.NaN)).toHaveLength(3);
    expect((await store.listTracesByChat(b.id)).map((t) => t.id)).toEqual(['tb']);
    expect(await store.listTracesByChat('ghost')).toEqual([]);
    expect(await store.getTrace('ghost')).toBeNull();
  });
});
