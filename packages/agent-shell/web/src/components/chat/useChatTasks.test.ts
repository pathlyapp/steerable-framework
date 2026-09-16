/**
 * useChatTasks 契约：
 *   - 拉 GET /chats/:id/tasks，`task-updated` 推送后重拉；
 *   - `finished` 只收本次挂载期间跑到终态的任务——首屏已经终态的历史任务
 *     不算「刚跑完」，否则每次打开对话都会重弹一遍；
 *   - 两次刷新之间从建到完的任务算刚跑完（用户同样没看见它运行过）；
 *   - `dismissFinished` 只隐藏通知，不影响 `tasks` 与 `summary`。
 */
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LocalTask } from '@/lib/local-api';
import { useChatTasks } from './useChatTasks';

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
});

function makeTask(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    id: 'task-1',
    chatId: 'chat_1',
    task: '跑测试',
    status: 'running',
    answer: null,
    error: null,
    worktreePath: null,
    worktreeBranch: null,
    worktreeState: null,
    createdAt: '2026-09-13T01:00:00.000Z',
    updatedAt: '2026-09-13T01:01:00.000Z',
    ...overrides,
  };
}

function installBridge(initial: LocalTask[]) {
  let current = initial;
  const listeners: Array<(p: { chatId: string; taskId: string; status: string }) => void> = [];
  (window as { electron?: unknown }).electron = {
    localBackend: {
      request: vi.fn((input: { method: string; path: string }) => {
        if (input.method === 'GET' && input.path.endsWith('/tasks')) {
          return Promise.resolve({ tasks: current });
        }
        return Promise.reject(new Error(`unexpected request: ${input.path}`));
      }),
    },
    onTaskUpdated: (cb: (p: { chatId: string; taskId: string; status: string }) => void) => {
      listeners.push(cb);
      return () => listeners.splice(listeners.indexOf(cb), 1);
    },
  };
  return {
    async push(next: LocalTask[]) {
      current = next;
      await act(async () => {
        for (const cb of listeners) cb({ chatId: 'chat_1', taskId: 'task-1', status: 'x' });
      });
    },
  };
}

describe('useChatTasks', () => {
  it('首屏已终态的历史任务不算刚跑完', async () => {
    installBridge([makeTask({ id: 'a', status: 'completed' })]);
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    expect(result.current.finished).toEqual([]);
    expect(result.current.summary.total).toBe(1);
  });

  it('运行中的任务跑到终态时进 finished', async () => {
    const bridge = installBridge([makeTask({ id: 'a', status: 'running' })]);
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    expect(result.current.finished).toEqual([]);

    const done = makeTask({ id: 'a', status: 'completed', answer: '42 行' });
    await bridge.push([done]);

    await waitFor(() => expect(result.current.finished).toEqual([done]));
  });

  it('两次刷新之间从建到完的任务也算刚跑完', async () => {
    const bridge = installBridge([]);
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.tasks).toEqual([]));

    const flash = makeTask({ id: 'a', status: 'failed', error: '炸了' });
    await bridge.push([flash]);

    await waitFor(() => expect(result.current.finished).toEqual([flash]));
  });

  it('同一个终态不会重复进 finished', async () => {
    const bridge = installBridge([makeTask({ id: 'a', status: 'running' })]);
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.tasks).toHaveLength(1));

    const done = makeTask({ id: 'a', status: 'completed' });
    await bridge.push([done]);
    await waitFor(() => expect(result.current.finished).toHaveLength(1));

    await bridge.push([done]);
    expect(result.current.finished).toHaveLength(1);
  });

  it('dismissFinished 只隐藏通知，任务本身还在', async () => {
    const bridge = installBridge([makeTask({ id: 'a', status: 'running' })]);
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.tasks).toHaveLength(1));
    await bridge.push([makeTask({ id: 'a', status: 'failed', error: '炸了' })]);
    await waitFor(() => expect(result.current.finished).toHaveLength(1));

    act(() => result.current.dismissFinished('a'));

    expect(result.current.finished).toEqual([]);
    expect(result.current.tasks).toHaveLength(1);
    expect(result.current.summary.failed).toBe(1);
  });

  it('任务表读不到时按「没有任务」处理', async () => {
    (window as { electron?: unknown }).electron = {
      localBackend: { request: vi.fn(() => Promise.reject(new Error('sidecar 离线'))) },
      onTaskUpdated: () => () => {},
    };
    const { result } = renderHook(() => useChatTasks('chat_1'));

    await waitFor(() => expect(result.current.summary.total).toBe(0));
    expect(result.current.finished).toEqual([]);
  });
});
