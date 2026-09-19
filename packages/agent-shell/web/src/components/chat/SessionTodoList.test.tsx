import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SessionTodoList } from './SessionTodoList';
import type { SessionTodo } from './todo-list-model';

afterEach(cleanup);

const TODOS: SessionTodo[] = [
  { id: 'a', content: '调研仓库', status: 'completed' },
  { id: 'b', content: '写补丁', status: 'in_progress' },
  { id: 'c', content: '跑测试', status: 'pending' },
];

describe('SessionTodoList', () => {
  it('defaults to collapsed, with the current step checkbox and 执行到 n/m', () => {
    render(<SessionTodoList todos={TODOS} />);
    const header = screen.getByRole('button', { name: /写补丁/ });
    expect(header.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('todo-item-c')).toBeNull();
    expect(screen.getByText('写补丁')).toBeTruthy();
    expect(screen.getByTestId('todo-progress').textContent).toBe('执行到 2/3');
    expect(header.querySelector('svg')).toBeTruthy();
  });

  it('expands to the full checklist with completed items struck through', () => {
    render(<SessionTodoList todos={TODOS} />);
    fireEvent.click(screen.getByRole('button', { name: /写补丁/ }));
    expect(screen.getByRole('button', { name: /任务清单/ }).getAttribute('aria-expanded')).toBe(
      'true',
    );
    expect(screen.getByTestId('todo-item-a').getAttribute('data-status')).toBe('completed');
    expect(screen.getByTestId('todo-item-a').querySelector('.line-through')).toBeTruthy();
    expect(screen.getByTestId('todo-item-b').getAttribute('data-status')).toBe('in_progress');
    expect(screen.getByTestId('todo-progress').textContent).toBe('执行到 2/3');
  });

  it('shows 已完成 when every item is done', () => {
    const done = TODOS.map((todo) => ({ ...todo, status: 'completed' as const }));
    render(<SessionTodoList todos={done} />);
    expect(screen.getByTestId('todo-progress').textContent).toBe('3/3 已完成');
    expect(screen.getByText('跑测试')).toBeTruthy();
  });
});
