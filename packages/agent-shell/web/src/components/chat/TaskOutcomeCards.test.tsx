/**
 * TaskOutcomeCards 契约：后台任务的终态要出现在用户正在看的对话里。
 *   - 完成显示 answer、失败显示 error，任务名都在；
 *   - 「查看过程」把任务交给右侧推理面板，「忽略」只发出 taskId；
 *   - worktree 待合并的任务提示去任务按钮处理（卡片不重复那套操作）；
 *   - 一次结束太多任务时只留前几张，其余折成一行汇总。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LocalTask } from '@/lib/local-api';
import { TaskOutcomeCards } from './TaskOutcomeCards';

afterEach(cleanup);

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
    createdAt: '2026-09-13T01:00:00.000Z',
    updatedAt: '2026-09-13T01:01:00.000Z',
    ...overrides,
  };
}

function renderCards(tasks: LocalTask[]) {
  const onInspect = vi.fn();
  const onDismiss = vi.fn();
  render(<TaskOutcomeCards tasks={tasks} onInspect={onInspect} onDismiss={onDismiss} />);
  return { onInspect, onDismiss };
}

describe('TaskOutcomeCards', () => {
  it('没有终态任务时什么都不渲染', () => {
    renderCards([]);
    expect(screen.queryByTestId('task-outcome-cards')).toBeNull();
  });

  it('完成的任务显示任务名与结果', () => {
    renderCards([makeTask()]);

    expect(screen.getByText('统计仓库行数')).toBeTruthy();
    expect(screen.getByText('共 42 行。')).toBeTruthy();
    expect(
      document.querySelector('[data-task-outcome="task-1"]')?.getAttribute('data-status'),
    ).toBe('completed');
  });

  it('失败的任务显示错误原因', () => {
    renderCards([makeTask({ status: 'failed', answer: null, error: '依赖任务失败：abc' })]);

    expect(screen.getByText('依赖任务失败：abc')).toBeTruthy();
    expect(
      document.querySelector('[data-task-outcome="task-1"]')?.getAttribute('data-status'),
    ).toBe('failed');
  });

  it('「查看过程」交出整个任务，「忽略」交出 taskId', () => {
    const task = makeTask();
    const { onInspect, onDismiss } = renderCards([task]);

    fireEvent.click(screen.getByRole('button', { name: '查看过程' }));
    expect(onInspect).toHaveBeenCalledWith(task);

    fireEvent.click(screen.getByRole('button', { name: '忽略' }));
    expect(onDismiss).toHaveBeenCalledWith('task-1');
  });

  it('worktree 待合并时指回任务按钮，卡片自己不放合并操作', () => {
    renderCards([
      makeTask({
        worktreeState: 'pending',
        worktreePath: '/tmp/wt',
        worktreeBranch: 'steerable/fix',
      }),
    ]);

    expect(screen.getByText('steerable/fix')).toBeTruthy();
    expect(screen.getByText(/待合并 — 在标题栏的任务按钮里处理/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /合并/ })).toBeNull();
  });

  it('超过三个时只留前三张，其余折成一行汇总', () => {
    renderCards(
      Array.from({ length: 5 }, (_, i) => makeTask({ id: `t${i}`, task: `任务 ${i}` })),
    );

    expect(document.querySelectorAll('[data-task-outcome]')).toHaveLength(3);
    expect(screen.getByText(/另有 2 个后台任务已结束/)).toBeTruthy();
  });
});
