/**
 * TaskPanelModal 的接线契约（4.6a/4.6c）：
 *   - 打开时经 localBackend 拉 GET /chats/:id/tasks，按状态渲染
 *     运行中/已完成/失败徽标；
 *   - 展开行显示 answer / error；
 *   - worktree 任务（worktreeState=pending 且已完成）显示「合并到主仓」
 *     /「丢弃」，点击走 POST /tasks/:id/merge|discard 并刷新；
 *   - 主进程 task-updated 推送触发重拉；
 *   - Escape 关闭。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LocalTask } from '@/lib/local-api';
import { TaskPanelModal } from './TaskPanelModal';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

function makeTask(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    id: 'task-1',
    chatId: 'chat_1',
    task: '统计仓库行数',
    status: 'completed',
    answer: '共 42 行。',
    error: null,
    worktreePath: null,
    worktreeBranch: null,
    worktreeState: null,
    createdAt: '2026-09-08T01:00:00.000Z',
    updatedAt: '2026-09-08T01:01:00.000Z',
    ...overrides,
  };
}

function installBridge(tasks: LocalTask[]) {
  const request = vi.fn((input: { method: string; path: string; body?: unknown }) => {
    if (input.method === 'GET' && input.path.endsWith('/tasks')) {
      return Promise.resolve({ tasks });
    }
    if (input.method === 'POST' && /\/tasks\/[^/]+\/(merge|discard)$/.test(input.path)) {
      return Promise.resolve({ success: true, task: tasks[0] });
    }
    return Promise.reject(new Error(`unexpected request: ${input.method} ${input.path}`));
  });
  const taskUpdatedListeners: Array<
    (payload: { chatId: string; taskId: string }) => void
  > = [];
  (window as { electron?: unknown }).electron = {
    localBackend: { request },
    onTaskUpdated: (
      cb: (payload: { chatId: string; taskId: string }) => void,
    ) => {
      taskUpdatedListeners.push(cb);
      return () => {
        const i = taskUpdatedListeners.indexOf(cb);
        if (i >= 0) taskUpdatedListeners.splice(i, 1);
      };
    },
  };
  return {
    request,
    emitTaskUpdated: (payload: { chatId: string; taskId: string }) => {
      for (const cb of taskUpdatedListeners) cb(payload);
    },
  };
}

describe('TaskPanelModal', () => {
  it('渲染任务列表：状态徽标 + 任务文本', async () => {
    installBridge([
      makeTask({ id: 't1', task: '跑测试', status: 'running', answer: null }),
      makeTask({ id: 't2', task: '写文档', status: 'completed' }),
      makeTask({ id: 't3', task: '改代码', status: 'failed', error: 'sidecar 断开' }),
    ]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} />);

    await waitFor(() => expect(screen.queryByText('正在加载任务…')).toBeNull());
    const rows = screen
      .getAllByRole('button', { hidden: true })
      .map((b) => b.closest('[data-task-row]'))
      .filter((el): el is HTMLElement => el !== null);
    const uniqueRows = [...new Set(rows)];
    expect(uniqueRows).toHaveLength(3);
    expect(uniqueRows.map((r) => r.getAttribute('data-status'))).toEqual([
      'running',
      'completed',
      'failed',
    ]);
    expect(screen.getByText('跑测试')).toBeTruthy();
    expect(screen.getByText(/3 个任务，1 个运行中/)).toBeTruthy();
  });

  it('空态文案', async () => {
    installBridge([]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} />);
    await waitFor(() => screen.getByText(/暂无后台任务/));
  });

  it('展开行显示答案；worktree pending 任务显示合并/丢弃并走对应路由', async () => {
    const { request } = installBridge([
      makeTask({
        id: 'wt-1',
        worktreePath: '/repo/.steerable/worktrees/demo',
        worktreeBranch: 'steerable/demo',
        worktreeState: 'pending',
      }),
    ]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} />);
    await waitFor(() => screen.getByText('统计仓库行数'));

    // 展开 → 答案 + worktree 操作。
    fireEvent.click(screen.getByRole('button', { name: '展开' }));
    await waitFor(() => screen.getByText('共 42 行。'));
    expect(screen.getByText('steerable/demo')).toBeTruthy();

    fireEvent.click(screen.getByText('合并到主仓'));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith({
        method: 'POST',
        path: '/api/v2/tasks/wt-1/merge',
      }),
    );
    // 合并后重拉列表。
    await waitFor(() =>
      expect(
        request.mock.calls.filter(
          ([input]) =>
            (input as { method: string }).method === 'GET' &&
            (input as { path: string }).path.endsWith('/tasks'),
        ).length,
      ).toBeGreaterThanOrEqual(2),
    );
  });

  it('丢弃走 discard 路由', async () => {
    const { request } = installBridge([
      makeTask({
        id: 'wt-2',
        worktreePath: '/repo/.steerable/worktrees/x',
        worktreeBranch: 'steerable/x',
        worktreeState: 'pending',
      }),
    ]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} />);
    await waitFor(() => screen.getByText('统计仓库行数'));

    fireEvent.click(screen.getByRole('button', { name: '展开' }));
    fireEvent.click(await screen.findByText('丢弃'));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith({
        method: 'POST',
        path: '/api/v2/tasks/wt-2/discard',
      }),
    );
  });

  it('task-updated 推送触发重拉；其他 chat 的推送忽略', async () => {
    const { request, emitTaskUpdated } = installBridge([makeTask()]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} />);
    await waitFor(() => screen.getByText('统计仓库行数'));
    const getsBefore = request.mock.calls.filter(
      ([input]) => (input as { method: string }).method === 'GET',
    ).length;

    emitTaskUpdated({ chatId: 'chat_2', taskId: 'x' });
    emitTaskUpdated({ chatId: 'chat_1', taskId: 'task-1' });

    await waitFor(() => {
      const gets = request.mock.calls.filter(
        ([input]) => (input as { method: string }).method === 'GET',
      ).length;
      expect(gets).toBe(getsBefore + 1);
    });
  });

  it('点击任务名调用 onInspect', async () => {
    const onInspect = vi.fn();
    installBridge([makeTask({ id: 't1', task: '跑测试', status: 'running', answer: null })]);
    render(<TaskPanelModal chatId="chat_1" onClose={() => {}} onInspect={onInspect} />);
    await waitFor(() => screen.getByText('跑测试'));
    fireEvent.click(screen.getByRole('button', { name: '跑测试' }));
    expect(onInspect).toHaveBeenCalledTimes(1);
    expect(onInspect.mock.calls[0][0]).toMatchObject({ id: 't1', task: '跑测试' });
  });

  it('Escape 关闭模态', async () => {
    const onClose = vi.fn();
    installBridge([makeTask()]);
    render(<TaskPanelModal chatId="chat_1" onClose={onClose} />);
    await waitFor(() => screen.getByText('统计仓库行数'));

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
