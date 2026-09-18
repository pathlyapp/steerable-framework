/**
 * useChatsAndAgents：侧栏状态钩子（分页会话列表 + 智能体目录 + 新建默认选中）。
 * 它是 useChatList 的 Electron 适配层，这里锁定适配层自己的契约：
 *   - 非 Electron 环境完全不发请求（skipInitialLoad + 传输层短路），
 *     create/delete 软失败并给出错误文案；
 *   - Electron 下首屏拉 page 1 与目录，目录到达后默认选中 local-assistant；
 *   - createChat 成功返回 id、失败返回 null 且错误上浮为字符串；
 *   - deleteChat 成功后重置回 page 1 重新拉取；
 *   - patchChatTitle 只改内存、不打后端（SSE 标题推送的配套路径）。
 * 桥走真实的 window.electron 路径，不 mock 模块。
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useChatsAndAgents } from './useChatsAndAgents';

type RequestInput = { method: string; path: string; body?: unknown };

const PAGE_1 = {
  chats: [
    { id: 'c1', title: '会话一' },
    { id: 'c2', title: '会话二' },
  ],
  pagination: { page: 1, limit: 50, total: 3, totalPages: 2, hasMore: true },
};
const PAGE_2 = {
  chats: [{ id: 'c3', title: '会话三' }],
  pagination: { page: 2, limit: 50, total: 3, totalPages: 2, hasMore: false },
};
const AGENTS = [{ id: 'agent-x' }, { id: 'local-assistant' }];

/** 装一个按路径路由的 request 替身，返回调用记录便于断言。 */
function installBridge(overrides: {
  onCreate?: () => Promise<unknown>;
  onDelete?: () => Promise<unknown>;
} = {}) {
  const calls: RequestInput[] = [];
  const request = vi.fn((input: RequestInput): Promise<unknown> => {
    calls.push(input);
    if (input.path.startsWith('/api/v2/chats?')) {
      return Promise.resolve(input.path.includes('page=2') ? PAGE_2 : PAGE_1);
    }
    if (input.path === '/api/v2/chat-agents') return Promise.resolve({ agents: AGENTS });
    if (input.path === '/api/v2/chats/new') {
      return overrides.onCreate ? overrides.onCreate() : Promise.resolve({ chatId: 'c-new' });
    }
    if (input.method === 'DELETE' && input.path.startsWith('/api/v2/chats/')) {
      return overrides.onDelete
        ? overrides.onDelete()
        : Promise.resolve({ success: true, message: '', chatId: 'c1' });
    }
    return Promise.reject(new Error(`unexpected ${input.method} ${input.path}`));
  });
  (window as { electron?: unknown }).electron = { localBackend: { request } };
  return { calls, request };
}

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
});

describe('非 Electron 环境', () => {
  it('不发任何请求，列表为空', async () => {
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.chats).toEqual([]);
    expect(result.current.agents).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it('createChat / deleteChat 软失败并上浮错误', async () => {
    const { result } = renderHook(() => useChatsAndAgents());
    let created: string | null = 'unset';
    await act(async () => {
      created = await result.current.createChat();
    });
    expect(created).toBeNull();
    expect(result.current.error).toBe('not in electron');
    let deleted = true;
    await act(async () => {
      deleted = await result.current.deleteChat('c1');
    });
    expect(deleted).toBe(false);
  });
});

describe('Electron 环境', () => {
  it('首屏拉取会话 page 1 与智能体目录，默认选中 local-assistant', async () => {
    installBridge();
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.chats).toHaveLength(2));
    expect(result.current.chats.map((c) => c.id)).toEqual(['c1', 'c2']);
    expect(result.current.hasMoreChats).toBe(true);
    await waitFor(() => expect(result.current.selectedAgentId).toBe('local-assistant'));
  });

  it('loadMoreChats 追加 page 2，hasMore 随响应翻转为 false', async () => {
    installBridge();
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.chats).toHaveLength(2));
    await act(async () => {
      await result.current.loadMoreChats();
    });
    expect(result.current.chats.map((c) => c.id)).toEqual(['c1', 'c2', 'c3']);
    expect(result.current.hasMoreChats).toBe(false);
  });

  it('createChat 成功返回新 id', async () => {
    installBridge();
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    let id: string | null = null;
    await act(async () => {
      id = await result.current.createChat({ agentId: 'local-assistant' });
    });
    expect(id).toBe('c-new');
    expect(result.current.error).toBeNull();
  });

  it('createChat 失败返回 null 且错误上浮为字符串', async () => {
    installBridge({ onCreate: () => Promise.reject(new Error('创建失败')) });
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    let id: string | null = 'unset';
    await act(async () => {
      id = await result.current.createChat();
    });
    expect(id).toBeNull();
    expect(result.current.error).toBe('创建失败');
  });

  it('deleteChat 成功后重置回 page 1 重新拉取', async () => {
    const { calls } = installBridge();
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.chats).toHaveLength(2));
    await act(async () => {
      await result.current.loadMoreChats();
    });
    expect(result.current.chats).toHaveLength(3);
    let ok = false;
    await act(async () => {
      ok = await result.current.deleteChat('c3');
    });
    expect(ok).toBe(true);
    expect(calls.some((c) => c.method === 'DELETE' && c.path === '/api/v2/chats/c3')).toBe(true);
    // 删除后 refresh 重置分页：重新拉了 page 1，列表回到两条。
    await waitFor(() => expect(result.current.chats.map((c) => c.id)).toEqual(['c1', 'c2']));
  });

  it('patchChatTitle 只改内存，不发请求', async () => {
    const { calls } = installBridge();
    const { result } = renderHook(() => useChatsAndAgents());
    await waitFor(() => expect(result.current.chats).toHaveLength(2));
    const before = calls.length;
    act(() => {
      result.current.patchChatTitle('c1', '新标题');
    });
    expect(result.current.chats[0].title).toBe('新标题');
    expect(calls.length).toBe(before);
    // 找不到的 id 静默忽略。
    act(() => {
      result.current.patchChatTitle('nope', '无');
    });
    expect(result.current.chats).toHaveLength(2);
  });
});
