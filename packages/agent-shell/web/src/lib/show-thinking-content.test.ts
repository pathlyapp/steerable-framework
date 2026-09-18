/**
 * 显示思考内容：默认关；localStorage 缺失时读关、写入不抛；
 * 只有存储值 '1' 才打开。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SHOW_THINKING_CONTENT_STORAGE_KEY,
  persistShowThinkingContent,
  readShowThinkingContent,
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

describe('readShowThinkingContent / persistShowThinkingContent', () => {
  it('localStorage 缺失时读回 false，写入静默不抛', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(readShowThinkingContent()).toBe(false);
    expect(() => persistShowThinkingContent(true)).not.toThrow();
  });

  it('无存储值时默认不显示思考内容', () => {
    stubLocalStorage();
    expect(readShowThinkingContent()).toBe(false);
  });

  it('只有 1 才打开，写入约定 key', () => {
    const store = stubLocalStorage();
    persistShowThinkingContent(true);
    expect(store.get(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('1');
    expect(readShowThinkingContent()).toBe(true);
    persistShowThinkingContent(false);
    expect(store.get(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('0');
    expect(readShowThinkingContent()).toBe(false);
  });

  it('存储里是非法值时读回 false', () => {
    const store = stubLocalStorage();
    store.set(SHOW_THINKING_CONTENT_STORAGE_KEY, 'yes');
    expect(readShowThinkingContent()).toBe(false);
  });
});
