/**
 * exec-policy：会话级命令执行沙箱档位的解析与持久化。
 * 锁定三点：只有精确字符串 'full' 才放开沙箱（其余一律落回 workspace）；
 * localStorage 缺失时（SSR / 无存储环境）读回缺省、写入静默不抛；
 * 有存储时读写往返不漂移。持久化路径用 vi.stubGlobal 装内存实现来测，
 * 缺失路径用 stubGlobal(..., undefined) 模拟。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  EXEC_POLICY_STORAGE_KEY,
  parseExecPolicy,
  persistExecPolicy,
  readStoredExecPolicy,
} from './exec-policy';

afterEach(() => {
  vi.unstubAllGlobals();
});

/** 装一个最小内存 localStorage，返回底层 Map 便于断言。 */
function stubLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
  return store;
}

describe('parseExecPolicy', () => {
  it("只有精确的 'full' 解析为 full", () => {
    expect(parseExecPolicy('full')).toBe('full');
  });

  it('其余一律落回 workspace（含大小写变体与 falsy）', () => {
    for (const value of ['workspace', 'FULL', 'Full', '', null, undefined, 0, 1, {}]) {
      expect(parseExecPolicy(value)).toBe('workspace');
    }
  });
});

describe('readStoredExecPolicy / persistExecPolicy', () => {
  it('localStorage 缺失时读回缺省 workspace，写入静默不抛', () => {
    // vitest.setup 会装内存 localStorage；stub 成 undefined 模拟 SSR 环境。
    vi.stubGlobal('localStorage', undefined);
    expect(readStoredExecPolicy()).toBe('workspace');
    expect(() => persistExecPolicy('full')).not.toThrow();
  });

  it('无存储值时默认 workspace', () => {
    stubLocalStorage();
    expect(readStoredExecPolicy()).toBe('workspace');
  });

  it('持久化后可读回，且写在约定的 key 上', () => {
    const store = stubLocalStorage();
    persistExecPolicy('full');
    expect(store.get(EXEC_POLICY_STORAGE_KEY)).toBe('full');
    expect(readStoredExecPolicy()).toBe('full');
    persistExecPolicy('workspace');
    expect(readStoredExecPolicy()).toBe('workspace');
  });

  it('存储里是非法值时读回 workspace', () => {
    const store = stubLocalStorage();
    store.set(EXEC_POLICY_STORAGE_KEY, 'everything');
    expect(readStoredExecPolicy()).toBe('workspace');
  });
});
