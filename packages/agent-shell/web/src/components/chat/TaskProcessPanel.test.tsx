/**
 * TaskProcessPanel：拉 GET /tasks/:id/process 渲染时间线；
 * task-process 广播更新 live 块。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TaskProcessPanel } from './TaskProcessPanel';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
  vi.restoreAllMocks();
});

function installBridge(timeline: unknown[], live = false, stale = false) {
  const request = vi.fn((input: { method: string; path: string }) => {
    if (input.method === 'GET' && input.path.endsWith('/process')) {
      return Promise.resolve({
        task: {
          id: 'task-1',
          chatId: 'chat_1',
          task: '问好循环',
          status: live ? 'running' : 'completed',
        },
        timeline,
        live,
        stale,
      });
    }
    return Promise.reject(new Error(`unexpected ${input.method} ${input.path}`));
  });
  const listeners: Array<
    (payload: { chatId: string; taskId: string; timeline: unknown; live: boolean }) => void
  > = [];
  (window as { electron?: unknown }).electron = {
    localBackend: { request },
    onTaskProcess: (
      cb: (payload: { chatId: string; taskId: string; timeline: unknown; live: boolean }) => void,
    ) => {
      listeners.push(cb);
      return () => {
        const i = listeners.indexOf(cb);
        if (i >= 0) listeners.splice(i, 1);
      };
    },
  };
  return {
    emit: (payload: { chatId: string; taskId: string; timeline: unknown; live: boolean }) => {
      for (const cb of listeners) cb(payload);
    },
  };
}

describe('TaskProcessPanel', () => {
  it('渲染历史推理与工具调用', async () => {
    installBridge([
      { type: 'reasoning', content: '先跑命令。' },
      { type: 'tools', actions: [{ tool: 'local_exec_shell', success: true }] },
      { type: 'text', content: '问好完成。' },
    ]);
    render(
      <TaskProcessPanel
        inspected={{ id: 'task-1', chatId: 'chat_1', title: '问好循环' }}
        onClose={() => {}}
      />,
    );
    await waitFor(() => screen.getByText('问好完成。'));
    expect(screen.getByText(/工具调用 1 次/)).toBeTruthy();
    expect(screen.getByText(/已结束/)).toBeTruthy();
  });

  it('task-process 推送更新时间线', async () => {
    const { emit } = installBridge([], true, false);
    render(
      <TaskProcessPanel
        inspected={{ id: 'task-1', chatId: 'chat_1', title: '问好循环' }}
        onClose={() => {}}
      />,
    );
    await waitFor(() => screen.getByText(/正在推理/));
    emit({
      chatId: 'chat_1',
      taskId: 'task-1',
      live: true,
      timeline: [{ type: 'reasoning', content: '开始第一轮。' }],
    });
    await waitFor(() => screen.getByText('开始第一轮。'));
  });

  it('live 增量时把滚动条钉在底部', async () => {
    let pinned = 0;
    const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop');
    Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
      configurable: true,
      get() {
        return desc?.get?.call(this) ?? 0;
      },
      set(value: number) {
        pinned += 1;
        desc?.set?.call(this, value);
      },
    });
    try {
      const { emit } = installBridge([], true, false);
      render(
        <TaskProcessPanel
          inspected={{ id: 'task-1', chatId: 'chat_1', title: '问好循环' }}
          onClose={() => {}}
        />,
      );
      await waitFor(() => screen.getByText(/正在推理/));
      pinned = 0;

      emit({
        chatId: 'chat_1',
        taskId: 'task-1',
        live: true,
        timeline: [{ type: 'reasoning', content: '开始第一轮。' }],
      });
      await waitFor(() => screen.getByText('开始第一轮。'));
      expect(pinned).toBeGreaterThan(0);
    } finally {
      if (desc) Object.defineProperty(HTMLElement.prototype, 'scrollTop', desc);
    }
  });
});
