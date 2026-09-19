import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ToolsFlow } from './ExecutedActionsCard';

afterEach(cleanup);

describe('ToolsFlow / todo_write', () => {
  it('keeps the compact tool row in the turn process, not a checklist', () => {
    render(
      <ToolsFlow
        actions={[
          {
            tool: 'todo_write',
            arguments: {
              todos: [
                { id: 'a', content: '调研仓库', status: 'completed' },
                { id: 'b', content: '写补丁', status: 'in_progress' },
              ],
            },
            result: { success: true },
          },
        ]}
      />,
    );

    expect(screen.getByText('todo_write')).toBeTruthy();
    expect(screen.getByText('已完成')).toBeTruthy();
    expect(screen.queryByTestId('todo-write-card')).toBeNull();
    expect(screen.queryByTestId('todo-item-a')).toBeNull();
    expect(screen.queryByText('调研仓库')).toBeNull();
  });
});
