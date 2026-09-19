/**
 * 显示思考内容：默认「显示5行」；兼容旧开关 0/1；
 * localStorage 缺失时读默认、写入不抛。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SHOW_THINKING_CONTENT_STORAGE_KEY,
  persistThinkingDisplay,
  readThinkingDisplay,
} from './show-thinking-content';

afterEach(() => {
  vi.unstubAllGlobals();
});

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

describe('readThinkingDisplay / persistThinkingDisplay', () => {
  it('localStorage 缺失时读回 peek，写入静默不抛', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(readThinkingDisplay()).toBe('peek');
    expect(() => persistThinkingDisplay('full')).not.toThrow();
  });

  it('无存储值时默认显示 5 行', () => {
    stubLocalStorage();
    expect(readThinkingDisplay()).toBe('peek');
  });

  it('写入三档并按约定 key 读回', () => {
    const store = stubLocalStorage();
    persistThinkingDisplay('hidden');
    expect(store.get(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('hidden');
    expect(readThinkingDisplay()).toBe('hidden');
    persistThinkingDisplay('peek');
    expect(readThinkingDisplay()).toBe('peek');
    persistThinkingDisplay('full');
    expect(readThinkingDisplay()).toBe('full');
  });

  it('兼容旧开关 1/0', () => {
    const store = stubLocalStorage();
    store.set(SHOW_THINKING_CONTENT_STORAGE_KEY, '1');
    expect(readThinkingDisplay()).toBe('full');
    store.set(SHOW_THINKING_CONTENT_STORAGE_KEY, '0');
    expect(readThinkingDisplay()).toBe('peek');
  });

  it('存储里是非法值时读回 peek', () => {
    const store = stubLocalStorage();
    store.set(SHOW_THINKING_CONTENT_STORAGE_KEY, 'yes');
    expect(readThinkingDisplay()).toBe('peek');
  });
});
