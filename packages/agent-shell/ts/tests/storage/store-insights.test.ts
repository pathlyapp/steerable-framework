/**
 * LocalStore「帮助改进产品」（insights_settings + insights_outbox）集成测试。
 *
 * 覆盖：设置的读取/合并/幂等（installId 一经生成不被 patch 覆盖、profile
 * 深合并）、outbox 的入队/过滤/分页 clamp、上传标记与错误截断、统计
 * 聚合并最终落到 exportInsightsBundle 的导出包 schema。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

const { LocalStore } = await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
  vi.useRealTimers();
});

describe('LocalStore / insights 设置', () => {
  it('从未配置过时 getInsightsSettings 返回 null', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.getInsightsSettings()).toBeNull();
  });

  it('ensureInsightsSettings 生成默认设置：三开关全关、installId 是 uuid', () => {
    const { store } = createTestStore(LocalStore);
    const settings = store.ensureInsightsSettings();
    expect(settings.installId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(settings.shareBehavior).toBe(false);
    expect(settings.shareConversation).toBe(false);
    expect(settings.shareProfile).toBe(false);
    expect(settings.promptedAt).toBeUndefined();
    expect(settings.apiBase).toBeUndefined();
    expect(settings.profile).toEqual({ displayName: '', email: '', company: '', note: '' });
    // 幂等：再调一次 installId 不变。
    expect(store.ensureInsightsSettings().installId).toBe(settings.installId);
  });

  it('setInsightsSettings 部分合并：未传的开关保持原值', () => {
    const { store } = createTestStore(LocalStore);
    store.setInsightsSettings({ shareBehavior: true });
    const after = store.setInsightsSettings({ shareConversation: true });
    expect(after.shareBehavior).toBe(true);
    expect(after.shareConversation).toBe(true);
    expect(after.shareProfile).toBe(false);
  });

  it('installId 一经生成不被后续 patch 覆盖', () => {
    const { store } = createTestStore(LocalStore);
    const first = store.ensureInsightsSettings();
    const after = store.setInsightsSettings({
      installId: '11111111-1111-4111-8111-111111111111',
    });
    expect(after.installId).toBe(first.installId);
  });

  it('profile 深合并：分两次 patch 的字段都保留', () => {
    const { store } = createTestStore(LocalStore);
    store.setInsightsSettings({ profile: { displayName: '王工' } });
    const after = store.setInsightsSettings({ profile: { email: 'a@b.c' } });
    expect(after.profile.displayName).toBe('王工');
    expect(after.profile.email).toBe('a@b.c');
  });

  it('settings_kv 里存了坏 JSON 时读为 null（不炸启动路径）', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(`INSERT INTO settings_kv (key, value) VALUES ('insights_settings', ?)`)
      .run('not-json');
    expect(store.getInsightsSettings()).toBeNull();
  });
});

describe('LocalStore / insights outbox', () => {
  it('enqueueInsight 落库字段：未上传、无错误、createdAt 是 ISO', () => {
    const { store } = createTestStore(LocalStore);
    const row = store.enqueueInsight('event', { name: 'app_start' });
    expect(row.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(row.kind).toBe('event');
    expect(row.payload).toEqual({ name: 'app_start' });
    expect(row.uploadedAt).toBeNull();
    expect(row.uploadError).toBeNull();
    expect(new Date(row.createdAt).toString()).not.toBe('Invalid Date');
  });

  it('listInsightOutbox 按 kind / uploaded 过滤，created_at 新→旧', () => {
    vi.useFakeTimers();
    const { store } = createTestStore(LocalStore);
    vi.setSystemTime('2026-01-01T00:00:01.000Z');
    const e1 = store.enqueueInsight('event', { n: 1 });
    vi.setSystemTime('2026-01-01T00:00:02.000Z');
    const t1 = store.enqueueInsight('turn', { n: 2 });
    vi.setSystemTime('2026-01-01T00:00:03.000Z');
    store.enqueueInsight('profile', { n: 3 });

    expect(store.listInsightOutbox().map((r) => r.id)).toHaveLength(3);
    expect(store.listInsightOutbox({ kind: 'event' }).map((r) => r.id)).toEqual([e1.id]);
    expect(store.listInsightOutbox({ uploaded: false })).toHaveLength(3);
    expect(store.listInsightOutbox({ uploaded: true })).toEqual([]);

    store.markInsightUploaded(t1.id);
    expect(store.listInsightOutbox({ uploaded: true }).map((r) => r.id)).toEqual([t1.id]);
    expect(store.listInsightOutbox({ uploaded: false })).toHaveLength(2);
    // kind + uploaded 组合过滤。
    expect(store.listInsightOutbox({ kind: 'turn', uploaded: true })).toHaveLength(1);
    expect(store.listInsightOutbox({ kind: 'turn', uploaded: false })).toEqual([]);
  });

  it('limit clamp：0 夹到 1，上限 500', () => {
    const { store } = createTestStore(LocalStore);
    store.enqueueInsight('event', {});
    store.enqueueInsight('event', {});
    expect(store.listInsightOutbox({ limit: 0 })).toHaveLength(1);
    expect(store.listInsightOutbox({ limit: 1 })).toHaveLength(1);

    // 上限 500：直写 510 行走事务，避免 510 次 enqueue 的噪音。
    const db = store.getPackDb();
    const insert = db.prepare(
      `INSERT INTO insights_outbox (id, kind, payload, created_at) VALUES (?, 'event', '{}', ?)`,
    );
    db.transaction(() => {
      for (let i = 0; i < 510; i += 1) insert.run(`bulk-${i}`, new Date(i * 1000).toISOString());
    })();
    expect(store.listInsightOutbox({ limit: 9999 })).toHaveLength(500);
  });

  it('markInsightUploaded 记录时间并清掉历史错误', () => {
    vi.useFakeTimers();
    const { store } = createTestStore(LocalStore);
    const row = store.enqueueInsight('turn', {});
    store.markInsightUploadError(row.id, '上一次失败');
    vi.setSystemTime('2026-01-01T12:00:00.000Z');
    store.markInsightUploaded(row.id);
    const after = store.listInsightOutbox({ uploaded: true })[0];
    expect(after.uploadedAt).toBe('2026-01-01T12:00:00.000Z');
    expect(after.uploadError).toBeNull();
  });

  it('markInsightUploadError 截断到 200 字符', () => {
    const { store } = createTestStore(LocalStore);
    const row = store.enqueueInsight('event', {});
    store.markInsightUploadError(row.id, 'x'.repeat(300));
    const after = store.listInsightOutbox()[0];
    expect(after.uploadError).toBe('x'.repeat(200));
  });

  it('坏行归一：未知 kind 读为 event，坏 payload 读为 {}', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(
        `INSERT INTO insights_outbox (id, kind, payload, created_at) VALUES (?, ?, ?, ?)`,
      )
      .run('bad-1', 'bogus-kind', 'not-json', new Date().toISOString());
    const [row] = store.listInsightOutbox();
    expect(row.kind).toBe('event');
    expect(row.payload).toEqual({});
  });
});

describe('LocalStore / insightStats 与导出包', () => {
  it('insightStats 按 kind 计数并统计 pending', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.insightStats()).toEqual({ events: 0, turns: 0, profile: 0, pending: 0 });
    const e1 = store.enqueueInsight('event', {});
    store.enqueueInsight('event', {});
    store.enqueueInsight('turn', {});
    store.enqueueInsight('profile', {});
    store.markInsightUploaded(e1.id);
    expect(store.insightStats()).toEqual({ events: 2, turns: 1, profile: 1, pending: 3 });
  });

  it('exportInsightsBundle：线协议 schema 常量与设置/统计/记录一致', () => {
    const { store } = createTestStore(LocalStore);
    store.setInsightsSettings({
      shareBehavior: true,
      profile: { displayName: '王工', company: 'ACME' },
    });
    store.enqueueInsight('event', { name: 'app_start' });
    store.enqueueInsight('turn', { chars: 42 });

    const bundle = store.exportInsightsBundle();
    // 线协议常量由遥测接收端定义，改名会破坏对端解析——照实断言现值。
    expect(bundle.schema).toBe('deeppath-agent-insights/v1');
    expect(new Date(bundle.exportedAt).toString()).not.toBe('Invalid Date');
    expect(bundle.installId).toBe(store.ensureInsightsSettings().installId);
    expect(bundle.settings).toEqual({
      shareBehavior: true,
      shareConversation: false,
      shareProfile: false,
    });
    expect(bundle.profile).toMatchObject({ displayName: '王工', company: 'ACME' });
    expect(bundle.stats).toEqual({ events: 1, turns: 1, profile: 0, pending: 2 });
    expect(bundle.records).toHaveLength(2);
    expect(bundle.records.map((r) => r.kind).sort()).toEqual(['event', 'turn']);
  });

  it('空 outbox 也能导出（records 为空数组）', () => {
    const { store } = createTestStore(LocalStore);
    const bundle = store.exportInsightsBundle();
    expect(bundle.schema).toBe('deeppath-agent-insights/v1');
    expect(bundle.records).toEqual([]);
    expect(bundle.stats).toEqual({ events: 0, turns: 0, profile: 0, pending: 0 });
  });
});
