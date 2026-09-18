/**
 * ChatHeader 任务角标契约：后台任务在面板关着的时候也要能被看见，
 * 且只有一件事等着处理时点角标要能跳过任务列表这一层。
 *   - 无任务 → 按钮保持素净，不显示计数；
 *   - 有任务 → 按钮上色 + 计数，配色按「该不该现在看一眼」取最高优先级
 *     （运行中 > 待合并 > 失败 > 全部跑完）；
 *   - 唯一失败任务 → 点角标直接开推理过程，不经弹层；
 *   - 唯一待合并任务 → 点角标开弹层并展开该行（合并/丢弃按钮在那儿）；
 *   - 有任务在跑或多件待处理 → 仍然先给列表，用户得先挑。
 * 列表本身的订阅与刷新归 useChatTasks，见 chat/useChatTasks.test.ts。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LocalChat, LocalTask } from '@/lib/local-api';
import { ChatHeader } from './ChatHeader';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

const CHAT: LocalChat = {
  id: 'chat_1',
  projectId: null,
  userId: 'u1',
  title: '循环任务',
  agentId: null,
  createdAt: '2026-09-13T01:00:00.000Z',
  updatedAt: '2026-09-13T01:00:00.000Z',
  isPinned: false,
  systemPrompt: null,
  pinnedRefs: null,
};

function makeTask(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    id: 'task-1',
    chatId: 'chat_1',
    task: '每 5 分钟问好',
    status: 'completed',
    answer: '你好',
    error: null,
    worktreePath: null,
    worktreeBranch: null,
    worktreeState: null,
    createdAt: '2026-09-13T01:00:00.000Z',
    updatedAt: '2026-09-13T01:01:00.000Z',
    ...overrides,
  };
}

/** 弹层自带订阅：展开它的用例需要一个能应答 GET /tasks 的 bridge。 */
function installBridge(tasks: LocalTask[]) {
  (window as { electron?: unknown }).electron = {
    localBackend: {
      request: vi.fn((input: { method: string; path: string }) => {
        if (input.method === 'GET' && input.path.endsWith('/tasks')) {
          return Promise.resolve({ tasks });
        }
        return Promise.reject(new Error(`unexpected request: ${input.path}`));
      }),
    },
    onTaskUpdated: () => () => {},
  };
}

function taskButton() {
  return screen.getByRole('button', { name: /后台任务/ });
}

describe('ChatHeader 后台任务角标', () => {
  it('没有任务时不显示计数', () => {
    render(<ChatHeader chat={CHAT} tasks={[]} />);

    expect(taskButton().getAttribute('data-task-state')).toBeNull();
    expect(taskButton().textContent).toBe('');
  });

  it('有运行中的任务时显示运行中计数并标成 running', () => {
    render(
      <ChatHeader
        chat={CHAT}
        tasks={[
          makeTask({ id: 'a', status: 'running' }),
          makeTask({ id: 'b', status: 'running' }),
          makeTask({ id: 'c', status: 'completed' }),
        ]}
      />,
    );

    expect(taskButton().getAttribute('data-task-state')).toBe('running');
    expect(taskButton().textContent).toBe('2');
    expect(taskButton().getAttribute('title')).toBe('共 3 个后台任务（2 个运行中）');
  });

  it('等依赖的任务也算在推进中', () => {
    render(
      <ChatHeader chat={CHAT} tasks={[makeTask({ id: 'a', status: 'blocked' })]} />,
    );

    expect(taskButton().getAttribute('data-task-state')).toBe('running');
    expect(taskButton().getAttribute('title')).toBe('共 1 个后台任务（1 个等依赖）');
  });

  it('待合并的 worktree 任务优先于失败任务上色', () => {
    render(
      <ChatHeader
        chat={CHAT}
        tasks={[
          makeTask({ id: 'a', status: 'completed', worktreeState: 'pending' }),
          makeTask({ id: 'b', status: 'failed', error: '炸了' }),
        ]}
      />,
    );

    expect(taskButton().getAttribute('data-task-state')).toBe('review');
    expect(taskButton().textContent).toBe('1');
    expect(taskButton().getAttribute('title')).toBe('共 2 个后台任务（1 个待合并，1 个失败）');
  });

  it('只有失败任务时标成 failed', () => {
    render(
      <ChatHeader
        chat={CHAT}
        tasks={[makeTask({ id: 'a', status: 'failed', error: '炸了' })]}
      />,
    );

    expect(taskButton().getAttribute('data-task-state')).toBe('failed');
  });

  it('全部跑完时显示总数，不再抢注意力', () => {
    render(
      <ChatHeader chat={CHAT} tasks={[makeTask({ id: 'a' }), makeTask({ id: 'b' })]} />,
    );

    expect(taskButton().getAttribute('data-task-state')).toBe('idle');
    expect(taskButton().textContent).toBe('2');
  });
});

describe('ChatHeader 角标直达', () => {
  it('唯一失败任务：点角标直接开推理过程，不开弹层', () => {
    const onInspectTask = vi.fn();
    render(
      <ChatHeader
        chat={CHAT}
        onInspectTask={onInspectTask}
        tasks={[makeTask({ id: 'a', status: 'failed', task: '跑测试', error: '炸了' })]}
      />,
    );

    expect(taskButton().getAttribute('data-task-shortcut')).toBe('process');
    expect(taskButton().getAttribute('title')).toContain('点击查看推理过程');

    fireEvent.click(taskButton());

    expect(onInspectTask).toHaveBeenCalledWith({
      id: 'a',
      chatId: 'chat_1',
      title: '跑测试',
    });
    expect(document.querySelector('[data-task-panel-modal]')).toBeNull();
  });

  it('唯一待合并任务：点角标开弹层并展开该行', async () => {
    const task = makeTask({
      id: 'a',
      status: 'completed',
      worktreeState: 'pending',
      worktreePath: '/tmp/wt',
      worktreeBranch: 'steerable/a',
    });
    installBridge([task]);
    render(<ChatHeader chat={CHAT} onInspectTask={vi.fn()} tasks={[task]} />);

    expect(taskButton().getAttribute('data-task-shortcut')).toBe('expand');
    expect(taskButton().getAttribute('title')).toContain('点击处理 worktree');

    fireEvent.click(taskButton());

    // 展开态的标志：worktree 的「合并到主仓」按钮直接可见，不用再点一次。
    await waitFor(() => expect(document.querySelector('[data-task-merge]')).not.toBeNull());
  });

  it('有任务在跑时不直达——先给列表', () => {
    const onInspectTask = vi.fn();
    render(
      <ChatHeader
        chat={CHAT}
        onInspectTask={onInspectTask}
        tasks={[
          makeTask({ id: 'a', status: 'failed', error: '炸了' }),
          makeTask({ id: 'b', status: 'running' }),
        ]}
      />,
    );

    expect(taskButton().getAttribute('data-task-shortcut')).toBeNull();
    expect(taskButton().getAttribute('title')).not.toContain('点击');
  });

  it('多件待处理时不直达——用户得先挑', () => {
    const onInspectTask = vi.fn();
    installBridge([]);
    render(
      <ChatHeader
        chat={CHAT}
        onInspectTask={onInspectTask}
        tasks={[
          makeTask({ id: 'a', status: 'failed', error: '炸了' }),
          makeTask({ id: 'b', status: 'failed', error: '也炸了' }),
        ]}
      />,
    );

    expect(taskButton().getAttribute('data-task-shortcut')).toBeNull();

    fireEvent.click(taskButton());

    expect(onInspectTask).not.toHaveBeenCalled();
    expect(document.querySelector('[data-task-panel-modal]')).not.toBeNull();
  });
});
