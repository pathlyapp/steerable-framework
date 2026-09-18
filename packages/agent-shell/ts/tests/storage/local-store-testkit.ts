/**
 * SqliteScopedStore 集成测试脚手架：真实 SQLite 临时库与目录隔离。
 * 每个用例经 createTestStore() 获得独占连接并由 cleanupTestStores()
 * 关闭连接、删除目录。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll } from 'vitest';
import Database from 'better-sqlite3';
import type { SqliteScopedStore } from '../../src/storage/index.js';
import { LOCAL_SCOPE } from '../../src/storage/driver.js';

/** Driver lifecycle tests restore this safe temporary data directory. */
const singletonDir = fs.mkdtempSync(path.join(os.tmpdir(), 'local-store-singleton-'));
process.env.DEEPPATH_USER_DATA_DIR = singletonDir;

// 本模块只被测试文件 import，afterAll 绑定到各测试文件自己的套件。
// 单例的库句柄与写租约随进程退出释放；目录在 POSIX 上可带句柄删除。
afterAll(() => {
  fs.rmSync(singletonDir, { recursive: true, force: true });
});

/** Loads the SQLite store implementation after the test data directory is set. */
export function loadStorageModule(): Promise<typeof import('../../src/storage/index.js')> {
  return import('../../src/storage/index.js');
}

export interface TestStoreHandle {
  store: SqliteScopedStore;
  db: Database.Database;
  /** 本用例独占的临时数据目录。 */
  dir: string;
  /** 主库文件路径（<dir>/agent-shell.db）。 */
  dbPath: string;
}

const openHandles: TestStoreHandle[] = [];

/** 在指定数据目录下同步执行 fn（例如构造第二个 store），结束后归位。 */
export function withDataDir<T>(dir: string, fn: () => T): T {
  process.env.DEEPPATH_USER_DATA_DIR = dir;
  try {
    return fn();
  } finally {
    process.env.DEEPPATH_USER_DATA_DIR = singletonDir;
  }
}

/**
 * 在独立临时目录里构造一个 SqliteScopedStore。
 */
export async function createTestStore(): Promise<TestStoreHandle> {
  const { SqliteScopedStore } = await loadStorageModule();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'local-store-'));
  const dbPath = path.join(dir, 'agent-shell.db');
  const db = new Database(dbPath);
  db.pragma('foreign_keys = ON');
  const store = new SqliteScopedStore(db, LOCAL_SCOPE);
  await store.initialize();
  const handle: TestStoreHandle = { store, db, dir, dbPath };
  openHandles.push(handle);
  return handle;
}

/** 关掉所有已开库并删除临时目录；在 afterEach 里调用。 */
export function cleanupTestStores(): void {
  while (openHandles.length) {
    const handle = openHandles.pop()!;
    try {
      handle.db.close();
    } catch {
      // 已关闭的库重复 close 会抛，清理路径不放大。
    }
    fs.rmSync(handle.dir, { recursive: true, force: true });
  }
  process.env.DEEPPATH_USER_DATA_DIR = singletonDir;
}
