import { describe, expect, it } from 'vitest';
import type { ExecutedAction } from './ExecutedActionsCard';
import {
  extractTodosFromAction,
  latestTodosFromActions,
  parseTodosPayload,
  resolveLatestSessionTodos,
  summarizeTodoWriteAction,
} from './todo-list-model';

const TODOS = [
  { id: 'a', content: '调研仓库', status: 'completed' as const },
  { id: 'b', content: '写补丁', status: 'in_progress' as const },
  { id: 'c', content: '跑测试', status: 'pending' as const },
];

describe('parseTodosPayload', () => {
  it('reads a bare todos array and the sidecar envelope', () => {
    expect(parseTodosPayload({ todos: TODOS })?.map((t) => t.id)).toEqual(['a', 'b', 'c']);
    expect(
      parseTodosPayload({
        success: true,
        data: { value: { todos: TODOS, summary: { total: 3 } } },
      })?.map((t) => t.status),
    ).toEqual(['completed', 'in_progress', 'pending']);
  });

  it('parses a resultPreview JSON string', () => {
    const todos = parseTodosPayload(JSON.stringify({ todos: TODOS }));
    expect(todos?.[1].content).toBe('写补丁');
  });

  it('rejects a malformed item instead of half-rendering', () => {
    expect(
      parseTodosPayload({
        todos: [{ id: 'a', content: 'x', status: 'done' }],
      }),
    ).toBeNull();
  });
});

describe('extractTodosFromAction', () => {
  it('prefers the result list, then falls back to in-flight arguments', () => {
    expect(
      extractTodosFromAction({
        tool: 'todo_write',
        arguments: { todos: TODOS },
        result: { todos: [{ ...TODOS[0], status: 'completed' }] },
      })?.[0].status,
    ).toBe('completed');

    expect(
      extractTodosFromAction({
        tool: 'todo_write',
        arguments: { todos: TODOS },
      })?.map((t) => t.id),
    ).toEqual(['a', 'b', 'c']);
  });

  it('ignores other tools', () => {
    expect(
      extractTodosFromAction({
        tool: 'local_read_file',
        arguments: { todos: TODOS },
      }),
    ).toBeNull();
  });
});

describe('summarizeTodoWriteAction', () => {
  it('names the in-progress item', () => {
    expect(summarizeTodoWriteAction('todo_write', { todos: TODOS }, undefined)).toBe(
      '任务清单 1/3 · 写补丁',
    );
  });

  it('marks an all-completed list', () => {
    const done = TODOS.map((t) => ({ ...t, status: 'completed' as const }));
    expect(summarizeTodoWriteAction('todo_write', { todos: done }, undefined)).toBe(
      '任务清单 3/3 已完成',
    );
  });
});

describe('latestTodosFromActions / resolveLatestSessionTodos', () => {
  it('takes the last todo_write rewrite', () => {
    const actions: ExecutedAction[] = [
      { tool: 'todo_write', arguments: { todos: TODOS } },
      {
        tool: 'todo_write',
        arguments: {
          todos: TODOS.map((t) => ({ ...t, status: 'completed' as const })),
        },
      },
    ];
    expect(latestTodosFromActions(actions)?.every((t) => t.status === 'completed')).toBe(
      true,
    );
  });

  it('prefers the live turn over older messages', () => {
    const todos = resolveLatestSessionTodos({
      messages: [{ id: 'm1' }, { id: 'm2' }],
      executedActionsByMessageId: {
        m1: [
          {
            tool: 'todo_write',
            arguments: { todos: [{ id: 'old', content: '旧清单', status: 'pending' }] },
          },
        ],
      },
      currentTurnActions: [{ tool: 'todo_write', arguments: { todos: TODOS } }],
    });
    expect(todos?.map((t) => t.id)).toEqual(['a', 'b', 'c']);
  });

  it('walks historical messages newest-first when the live turn has no list', () => {
    const todos = resolveLatestSessionTodos({
      messages: [{ id: 'm1' }, { id: 'm2' }],
      executedActionsByMessageId: {
        m1: [
          {
            tool: 'todo_write',
            arguments: { todos: [{ id: 'old', content: '旧清单', status: 'pending' }] },
          },
        ],
        m2: [{ tool: 'todo_write', arguments: { todos: TODOS } }],
      },
    });
    expect(todos?.map((t) => t.id)).toEqual(['a', 'b', 'c']);
  });
});
