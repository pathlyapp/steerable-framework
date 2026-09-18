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

await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
  vi.useRealTimers();
});

describe('LocalStore / insights 设置', () => {
  it('从未配置过时 getInsightsSettings 返回 null', async () => {
    const { store, db } = await createTestStore();
    expect(await store.getInsightsSettings()).toBeNull();
  });

  it('ensureInsightsSettings 生成默认设置：三开关全关、installId 是 uuid', async () => {
    const { store, db } = await createTestStore();
    const settings = await store.ensureInsightsSettings();
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
    expect((await store.ensureInsightsSettings()).installId).toBe(settings.installId);
  });

  it('setInsightsSettings 部分合并：未传的开关保持原值', async () => {
    const { store, db } = await createTestStore();
    await store.setInsightsSettings({ shareBehavior: true });
    const after = await store.setInsightsSettings({ shareConversation: true });
    expect(after.shareBehavior).toBe(true);
    expect(after.shareConversation).toBe(true);
    expect(after.shareProfile).toBe(false);
  });

  it('installId 一经生成不被后续 patch 覆盖', async () => {
    const { store, db } = await createTestStore();
    const first = await store.ensureInsightsSettings();
    const after = await store.setInsightsSettings({
      installId: '11111111-1111-4111-8111-111111111111',
    });
    expect(after.installId).toBe(first.installId);
  });

  it('profile 深合并：分两次 patch 的字段都保留', async () => {
    const { store, db } = await createTestStore();
    await store.setInsightsSettings({ profile: { displayName: '王工' } });
    const after = await store.setInsightsSettings({ profile: { email: 'a@b.c' } });
    expect(after.profile.displayName).toBe('王工');
    expect(after.profile.email).toBe('a@b.c');
  });

  it('settings_kv 里存了坏 JSON 时读为 null（不炸启动路径）', async () => {
    const { store, db } = await createTestStore();
    db
      .prepare(`INSERT INTO settings_kv (tenant_id, user_id, key, value)
                VALUES ('local', 'local', 'insights_settings', ?)`)
      .run('not-json');
    expect(await store.getInsightsSettings()).toBeNull();
  });
});

describe('LocalStore / insights outbox', () => {
  it('enqueueInsight 落库字段：未上传、无错误、createdAt 是 ISO', async () => {
    const { store, db } = await createTestStore();
    const row = await store.enqueueInsight('event', { name: 'app_start' });
    expect(row.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(row.kind).toBe('event');
    expect(row.payload).toEqual({ name: 'app_start' });
    expect(row.uploadedAt).toBeNull();
    expect(row.uploadError).toBeNull();
    expect(new Date(row.createdAt).toString()).not.toBe('Invalid Date');
  });

  it('listInsightOutbox 按 kind / uploaded 过滤，created_at 新→旧', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    vi.setSystemTime('2026-01-01T00:00:01.000Z');
    const e1 = await store.enqueueInsight('event', { n: 1 });
    vi.setSystemTime('2026-01-01T00:00:02.000Z');
    const t1 = await store.enqueueInsight('turn', { n: 2 });
    vi.setSystemTime('2026-01-01T00:00:03.000Z');
    await store.enqueueInsight('profile', { n: 3 });

    expect((await store.listInsightOutbox()).map((r) => r.id)).toHaveLength(3);
    expect((await store.listInsightOutbox({ kind: 'event' })).map((r) => r.id)).toEqual([e1.id]);
    expect(await store.listInsightOutbox({ uploaded: false })).toHaveLength(3);
    expect(await store.listInsightOutbox({ uploaded: true })).toEqual([]);

    await store.markInsightUploaded(t1.id);
    expect((await store.listInsightOutbox({ uploaded: true })).map((r) => r.id)).toEqual([t1.id]);
    expect(await store.listInsightOutbox({ uploaded: false })).toHaveLength(2);
    // kind + uploaded 组合过滤。
    expect(await store.listInsightOutbox({ kind: 'turn', uploaded: true })).toHaveLength(1);
    expect(await store.listInsightOutbox({ kind: 'turn', uploaded: false })).toEqual([]);
  });

  it('limit clamp：0 夹到 1，上限 500', async () => {
    const { store, db } = await createTestStore();
    await store.enqueueInsight('event', {});
    await store.enqueueInsight('event', {});
    expect(await store.listInsightOutbox({ limit: 0 })).toHaveLength(1);
    expect(await store.listInsightOutbox({ limit: 1 })).toHaveLength(1);

    // 上限 500：直写 510 行走事务，避免 510 次 enqueue 的噪音。
    const insert = db.prepare(
        `INSERT INTO insights_outbox
          (tenant_id, user_id, id, kind, payload, created_at)
         VALUES ('local', 'local', ?, 'event', '{}', ?)`,
    );
    db.transaction(() => {
      for (let i = 0; i < 510; i += 1) insert.run(`bulk-${i}`, new Date(i * 1000).toISOString());
    })();
    expect(await store.listInsightOutbox({ limit: 9999 })).toHaveLength(500);
  });

  it('markInsightUploaded 记录时间并清掉历史错误', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    const row = await store.enqueueInsight('turn', {});
    await store.markInsightUploadError(row.id, '上一次失败');
    vi.setSystemTime('2026-01-01T12:00:00.000Z');
    await store.markInsightUploaded(row.id);
    const after = (await store.listInsightOutbox({ uploaded: true }))[0];
    expect(after.uploadedAt).toBe('2026-01-01T12:00:00.000Z');
    expect(after.uploadError).toBeNull();
  });

  it('markInsightUploadError 截断到 200 字符', async () => {
    const { store, db } = await createTestStore();
    const row = await store.enqueueInsight('event', {});
    await store.markInsightUploadError(row.id, 'x'.repeat(300));
    const after = (await store.listInsightOutbox())[0];
    expect(after.uploadError).toBe('x'.repeat(200));
  });

  it('坏行归一：未知 kind 读为 event，坏 payload 读为 {}', async () => {
    const { store, db } = await createTestStore();
    db
      .prepare(
        `INSERT INTO insights_outbox
          (tenant_id, user_id, id, kind, payload, created_at)
         VALUES ('local', 'local', ?, ?, ?, ?)`,
      )
      .run('bad-1', 'bogus-kind', 'not-json', new Date().toISOString());
    const [row] = await store.listInsightOutbox();
    expect(row.kind).toBe('event');
    expect(row.payload).toEqual({});
  });
});

describe('LocalStore / insightStats 与导出包', () => {
  it('insightStats 按 kind 计数并统计 pending', async () => {
    const { store, db } = await createTestStore();
    expect(await store.insightStats()).toEqual({ events: 0, turns: 0, profile: 0, pending: 0 });
    const e1 = await store.enqueueInsight('event', {});
    await store.enqueueInsight('event', {});
    await store.enqueueInsight('turn', {});
    await store.enqueueInsight('profile', {});
    await store.markInsightUploaded(e1.id);
    expect(await store.insightStats()).toEqual({ events: 2, turns: 1, profile: 1, pending: 3 });
  });

  it('exportInsightsBundle：线协议 schema 常量与设置/统计/记录一致', async () => {
    const { store, db } = await createTestStore();
    await store.setInsightsSettings({
      shareBehavior: true,
      profile: { displayName: '王工', company: 'ACME' },
    });
    await store.enqueueInsight('event', { name: 'app_start' });
    await store.enqueueInsight('turn', { chars: 42 });

    const bundle = await store.exportInsightsBundle();
    // 线协议常量由遥测接收端定义，改名会破坏对端解析——照实断言现值。
    expect(bundle.schema).toBe('deeppath-agent-insights/v1');
    expect(new Date(bundle.exportedAt).toString()).not.toBe('Invalid Date');
    expect(bundle.installId).toBe((await store.ensureInsightsSettings()).installId);
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

  it('空 outbox 也能导出（records 为空数组）', async () => {
    const { store, db } = await createTestStore();
    const bundle = await store.exportInsightsBundle();
    expect(bundle.schema).toBe('deeppath-agent-insights/v1');
    expect(bundle.records).toEqual([]);
    expect(bundle.stats).toEqual({ events: 0, turns: 0, profile: 0, pending: 0 });
  });
});
