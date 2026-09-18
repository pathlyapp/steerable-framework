/**
 * LocalStore 集成测试脚手架：真实 SQLite（临时目录库文件）+ 依赖隔离。
 *
 * storage/index.js 在模块求值时就构造 localStore 单例（打开真实库文件并
 * 持有写租约），所以测试文件必须在动态 import 它之前把
 * DEEPPATH_USER_DATA_DIR 指到临时目录——否则单例会落到真实用户目录
 * （~/.agent-shell）：既污染本机数据，又可能撞上正在运行的应用持有的
 * 写租约而被 createLocalStore 的兜底逻辑直接 process.exit。本模块在
 * 求值时（早于测试文件主体里的 await loadStorageModule()）完成这次指向。
 *
 * 每个用例经 createTestStore() 拿一个独占临时目录的 LocalStore；
 * afterEach(cleanupTestStores) 关库删目录。写租约是 LocalStore 的私有
 * 字段、无 release API，随进程退出由内核释放；临时目录在 POSIX 上可带
 * 打开句柄删除，清理不受影响。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll } from 'vitest';
import type { LocalStore } from '../../src/storage/index.js';

/** localStore 单例的落脚目录（本测试文件进程内全局一份）。 */
const singletonDir = fs.mkdtempSync(path.join(os.tmpdir(), 'local-store-singleton-'));
process.env.DEEPPATH_USER_DATA_DIR = singletonDir;

// 本模块只被测试文件 import，afterAll 绑定到各测试文件自己的套件。
// 单例的库句柄与写租约随进程退出释放；目录在 POSIX 上可带句柄删除。
afterAll(() => {
  fs.rmSync(singletonDir, { recursive: true, force: true });
});

/** 动态加载 storage/index.js；此刻单例构造在上方准备好的临时目录里。 */
export function loadStorageModule(): Promise<typeof import('../../src/storage/index.js')> {
  return import('../../src/storage/index.js');
}

export interface TestStoreHandle {
  store: LocalStore;
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
 * 在独立临时目录里构造一个 LocalStore。getUserDataDir 在构造时读
 * DEEPPATH_USER_DATA_DIR，构造完立即归位，不影响后续用例。
 */
export function createTestStore(Store: new () => LocalStore): TestStoreHandle {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'local-store-'));
  const store = withDataDir(dir, () => new Store());
  const handle: TestStoreHandle = { store, dir, dbPath: path.join(dir, 'agent-shell.db') };
  openHandles.push(handle);
  return handle;
}

/** 关掉所有已开库并删除临时目录；在 afterEach 里调用。 */
export function cleanupTestStores(): void {
  while (openHandles.length) {
    const handle = openHandles.pop()!;
    try {
      handle.store.getPackDb().close();
    } catch {
      // 已关闭的库重复 close 会抛，清理路径不放大。
    }
    fs.rmSync(handle.dir, { recursive: true, force: true });
  }
  process.env.DEEPPATH_USER_DATA_DIR = singletonDir;
}
