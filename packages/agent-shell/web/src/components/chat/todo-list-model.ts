/**
 * Session task list — the UI half of `todo_write`.
 *
 * The tool is full-replace: each call carries the complete list. The latest
 * successful rewrite (or the in-flight arguments, before the result lands)
 * is the session header; older calls in the transcript stay as snapshots.
 */

import type { ExecutedAction } from './ExecutedActionsCard';
import type { TurnBlock } from './turn-timeline';

export const TODO_STATUSES = ['pending', 'in_progress', 'completed'] as const;
export type TodoStatus = (typeof TODO_STATUSES)[number];

export interface SessionTodo {
  id: string;
  content: string;
  status: TodoStatus;
}

export interface SessionTodoSummary {
  total: number;
  pending: number;
  inProgress: number;
  completed: number;
}

const STATUS_SET = new Set<string>(TODO_STATUSES);

function asObject(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null;
    try {
      return asObject(JSON.parse(trimmed) as unknown);
    } catch {
      return null;
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

function coerceTodos(raw: unknown): SessionTodo[] | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const todos: SessionTodo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') return null;
    const row = item as Record<string, unknown>;
    const id = typeof row.id === 'string' ? row.id : '';
    const content = typeof row.content === 'string' ? row.content.trim() : '';
    const status = row.status;
    if (!id || !content || typeof status !== 'string' || !STATUS_SET.has(status)) {
      return null;
    }
    todos.push({ id, content, status: status as TodoStatus });
  }
  return todos.length > 0 ? todos : null;
}

/** Pull a todos array out of args, a tool result, or the sidecar envelope. */
export function parseTodosPayload(value: unknown): SessionTodo[] | null {
  if (Array.isArray(value)) return coerceTodos(value);
  const obj = asObject(value);
  if (!obj) return null;
  if (Array.isArray(obj.todos)) return coerceTodos(obj.todos);
  const data = asObject(obj.data);
  if (data) {
    if (Array.isArray(data.todos)) return coerceTodos(data.todos);
    const inner = asObject(data.value);
    if (inner && Array.isArray(inner.todos)) return coerceTodos(inner.todos);
  }
  const wrapped = asObject(obj.value);
  if (wrapped && Array.isArray(wrapped.todos)) return coerceTodos(wrapped.todos);
  return null;
}

export function extractTodosFromAction(action: {
  tool: string;
  arguments?: unknown;
  result?: unknown;
}): SessionTodo[] | null {
  if (action.tool !== 'todo_write') return null;
  return parseTodosPayload(action.result) ?? parseTodosPayload(action.arguments);
}

export function summarizeTodos(todos: SessionTodo[]): SessionTodoSummary {
  const summary: SessionTodoSummary = {
    total: todos.length,
    pending: 0,
    inProgress: 0,
    completed: 0,
  };
  for (const todo of todos) {
    if (todo.status === 'pending') summary.pending += 1;
    else if (todo.status === 'in_progress') summary.inProgress += 1;
    else summary.completed += 1;
  }
  return summary;
}

export function summarizeTodoWriteAction(
  tool: string,
  args: unknown,
  result: unknown,
): string | null {
  if (tool !== 'todo_write') return null;
  const todos = extractTodosFromAction({ tool, arguments: args, result });
  if (!todos) return '任务清单';
  const { completed, total, inProgress } = summarizeTodos(todos);
  const current = todos.find((todo) => todo.status === 'in_progress');
  if (inProgress > 0 && current) {
    const label =
      current.content.length > 28 ? `${current.content.slice(0, 25)}…` : current.content;
    return `任务清单 ${completed}/${total} · ${label}`;
  }
  if (completed === total) return `任务清单 ${completed}/${total} 已完成`;
  return `任务清单 ${completed}/${total}`;
}

export function latestTodosFromActions(
  actions: Array<{ tool: string; arguments?: unknown; result?: unknown }> | undefined,
): SessionTodo[] | null {
  if (!actions || actions.length === 0) return null;
  for (let i = actions.length - 1; i >= 0; i -= 1) {
    const todos = extractTodosFromAction(actions[i]);
    if (todos) return todos;
  }
  return null;
}

function actionsFromTimeline(blocks: TurnBlock[] | undefined): ExecutedAction[] {
  if (!blocks) return [];
  const actions: ExecutedAction[] = [];
  for (const block of blocks) {
    if (block.type === 'tools') actions.push(...block.actions);
  }
  return actions;
}

/**
 * Newest `todo_write` list for the open chat. Live turn first, then
 * historical actions / timelines walking messages newest-first.
 */
export function resolveLatestSessionTodos(input: {
  messages: Array<{ id: string }>;
  executedActionsByMessageId?: Record<string, ExecutedAction[]>;
  currentTurnActions?: ExecutedAction[];
  timelineByMessageId?: Record<string, TurnBlock[]>;
  currentTurnTimeline?: TurnBlock[];
}): SessionTodo[] | null {
  const live =
    latestTodosFromActions(input.currentTurnActions) ??
    latestTodosFromActions(actionsFromTimeline(input.currentTurnTimeline));
  if (live) return live;

  for (let i = input.messages.length - 1; i >= 0; i -= 1) {
    const id = input.messages[i].id;
    const historical =
      latestTodosFromActions(input.executedActionsByMessageId?.[id]) ??
      latestTodosFromActions(actionsFromTimeline(input.timelineByMessageId?.[id]));
    if (historical) return historical;
  }
  return null;
}
