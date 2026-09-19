import { useMemo, useState } from 'react';
import {
  LuCircle,
  LuCircleCheck,
  LuCircleDot,
  LuChevronDown,
  LuChevronRight,
  LuListChecks,
} from 'react-icons/lu';
import {
  summarizeTodos,
  type SessionTodo,
  type TodoStatus,
} from './todo-list-model';

function StatusIcon({
  status,
  className,
}: {
  status: TodoStatus;
  className?: string;
}) {
  const cls = ['h-3.5 w-3.5 shrink-0', className].filter(Boolean).join(' ');
  if (status === 'completed') {
    return <LuCircleCheck className={`${cls} text-emerald-600 dark:text-emerald-400`} />;
  }
  if (status === 'in_progress') {
    return <LuCircleDot className={`${cls} text-blue-600 dark:text-blue-400`} />;
  }
  return <LuCircle className={`${cls} text-agent-muted-foreground/70`} />;
}

export function currentTodoStep(todos: SessionTodo[]): SessionTodo | null {
  return (
    todos.find((todo) => todo.status === 'in_progress') ??
    todos.find((todo) => todo.status === 'pending') ??
    todos[todos.length - 1] ??
    null
  );
}

export function todoProgressCopy(todos: SessionTodo[]): string {
  const summary = summarizeTodos(todos);
  if (summary.total === 0) return '';
  if (summary.completed === summary.total) {
    return `${summary.completed}/${summary.total} 已完成`;
  }
  const currentIndex = todos.findIndex((todo) => todo.status === 'in_progress');
  const step =
    currentIndex >= 0 ? currentIndex + 1 : Math.min(summary.completed + 1, summary.total);
  return `执行到 ${step}/${summary.total}`;
}

export function TodoItems({
  todos,
  compact = false,
}: {
  todos: SessionTodo[];
  compact?: boolean;
}) {
  return (
    <ol
      className={`m-0 list-none ${compact ? 'max-h-40 space-y-1 overflow-y-auto px-2.5 py-1.5' : 'space-y-1 px-2.5 py-1.5'}`}
    >
      {todos.map((todo) => {
        const done = todo.status === 'completed';
        const active = todo.status === 'in_progress';
        return (
          <li
            key={todo.id}
            data-testid={`todo-item-${todo.id}`}
            data-status={todo.status}
            className="flex items-start gap-2 text-[12px] leading-relaxed"
          >
            <StatusIcon status={todo.status} className="mt-[2px]" />
            <span
              className={
                done
                  ? 'text-agent-muted-foreground line-through'
                  : active
                    ? 'font-medium text-agent-foreground'
                    : 'text-agent-foreground'
              }
            >
              {todo.content}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

export function SessionTodoList({ todos }: { todos: SessionTodo[] }) {
  const [expanded, setExpanded] = useState(false);
  const current = useMemo(() => currentTodoStep(todos), [todos]);
  const progressLabel = useMemo(() => todoProgressCopy(todos), [todos]);

  return (
    <div
      className="mb-1 overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas shadow-sm"
      data-testid="session-todo-list"
    >
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left"
        aria-expanded={expanded}
      >
        {expanded ? (
          <LuChevronDown className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
        ) : (
          <LuChevronRight className="h-3.5 w-3.5 shrink-0 text-agent-muted-foreground" />
        )}
        {expanded || !current ? (
          <LuListChecks className="h-3.5 w-3.5 shrink-0 text-agent-foreground" />
        ) : (
          <StatusIcon status={current.status} />
        )}
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-agent-foreground">
          {expanded ? '任务清单' : (current?.content ?? '任务清单')}
        </span>
        {progressLabel ? (
          <span
            className="shrink-0 rounded-full bg-agent-muted px-1.5 py-0.5 text-[10px] font-medium text-agent-muted-foreground"
            data-testid="todo-progress"
          >
            {progressLabel}
          </span>
        ) : null}
      </button>
      {expanded ? <TodoItems todos={todos} compact /> : null}
    </div>
  );
}

export default SessionTodoList;
