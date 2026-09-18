/**
 * LocalStore 生命周期集成测试：库文件布局、pragma、写租约互斥、
 * 多库隔离与模块级 localStore 单例。
 *
 * 写租约是同路径换 .lock 后缀的旁路文件上的 BEGIN EXCLUSIVE（内核锁）：
 * 第二个 LocalStore 指向同一目录时必须立刻抛 StoreAlreadyOwnedError，
 * 而不是两个进程/实例共用一份 WAL 把库写坏。
 */
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { StoreAlreadyOwnedError } from '../../src/storage/write-lease.js';
import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
  withDataDir,
} from './local-store-testkit.js';

const { LocalStore, localStore } = await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
});

describe('LocalStore / 库文件布局与 pragma', () => {
  it('主库落在 <userData>/agent-shell.db，旁路锁文件同名 .lock', () => {
    const handle = createTestStore(LocalStore);
    expect(fs.existsSync(handle.dbPath)).toBe(true);
    expect(fs.existsSync(path.join(handle.dir, 'agent-shell.lock'))).toBe(true);
  });

  it('journal_mode = WAL 且 foreign_keys = ON', () => {
    const { store } = createTestStore(LocalStore);
    const journal = store.getPackDb().pragma('journal_mode') as Array<{ journal_mode: string }>;
    expect(journal[0].journal_mode).toBe('wal');
    const fk = store.getPackDb().pragma('foreign_keys') as Array<{ foreign_keys: number }>;
    expect(fk[0].foreign_keys).toBe(1);
  });

  it('migrate 建出全部核心表与索引', () => {
    const { store } = createTestStore(LocalStore);
    const rows = store
      .getPackDb()
      .prepare(`SELECT name FROM sqlite_master WHERE type IN ('table', 'index')`)
      .all() as Array<{ name: string }>;
    const names = new Set(rows.map((r) => r.name));
    for (const table of [
      'chat_sessions',
      'chat_messages',
      'chat_agents',
      'settings_kv',
      'harness_traces',
      'usage_events',
      'insights_outbox',
      'tasks',
    ]) {
      expect(names).toContain(table);
    }
    for (const index of [
      'idx_harness_traces_chat',
      'idx_usage_events_created',
      'idx_insights_outbox_pending',
      'idx_tasks_chat',
    ]) {
      expect(names).toContain(index);
    }
  });

  it('增量迁移列就位：project_id / depends_on / process_json / load_all_skills', () => {
    const { store } = createTestStore(LocalStore);
    const columnsOf = (table: string) =>
      new Set(
        (store.getPackDb().prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).map(
          (c) => c.name,
        ),
      );
    expect(columnsOf('chat_sessions').has('project_id')).toBe(true);
    expect(columnsOf('tasks').has('depends_on')).toBe(true);
    expect(columnsOf('tasks').has('process_json')).toBe(true);
    expect(columnsOf('chat_agents').has('load_all_skills')).toBe(true);
  });
});

describe('LocalStore / 写租约互斥', () => {
  it('同一数据目录上的第二个 LocalStore 立刻抛 StoreAlreadyOwnedError', () => {
    const handle = createTestStore(LocalStore);
    expect(() => withDataDir(handle.dir, () => new LocalStore())).toThrow(StoreAlreadyOwnedError);
    // 第一个实例不受影响，仍可正常读写。
    expect(handle.store.createChat('还活着').title).toBe('还活着');
  });

  it('不同数据目录的两个实例互不可见', () => {
    const a = createTestStore(LocalStore);
    const b = createTestStore(LocalStore);
    a.store.createChat('只在 A');
    expect(a.store.listChats().total).toBe(1);
    expect(b.store.listChats().total).toBe(0);
  });
});

describe('LocalStore / getPackDb 与模块级单例', () => {
  it('getPackDb 借出的句柄可直接执行 SQL（场景包存储扩展点）', () => {
    const { store } = createTestStore(LocalStore);
    const row = store.getPackDb().prepare(`SELECT 1 + 1 AS n`).get() as { n: number };
    expect(row.n).toBe(2);
  });

  it('模块级 localStore 单例已构造且可用（落在测试准备的临时目录）', () => {
    // 单例在 import storage/index.js 时即构造；testkit 已把它的数据目录
    // 指到临时目录，这里只验证它能正常服务。
    expect(localStore.listChats().total).toBeGreaterThanOrEqual(0);
    // 内置种子在单例库同样就位。
    expect(localStore.getChatAgent('local-assistant')?.name).toBe('电脑操作员');
  });
});
