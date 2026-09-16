import { describe, expect, it } from 'vitest';
import { timelineFromHistoryEntries } from '../../src/local-backend/task-process.js';

describe('timelineFromHistoryEntries', () => {
  it('projects assistant reasoning, tool calls, and results in call order', () => {
    const blocks = timelineFromHistoryEntries([
      { kind: 'system', message: { role: 'system', content: '你是后台任务' } },
      {
        kind: 'user',
        message: { role: 'user', content: [{ type: 'text', text: '问好' }] },
      },
      {
        kind: 'assistant',
        message: {
          role: 'assistant',
          content: [{ type: 'text', text: '' }],
          reasoning: '先睡 300 秒再问好。',
          tool_calls: [
            {
              id: 'call_1',
              name: 'local_exec_shell',
              arguments: { command: 'sleep 300; echo 你好', timeout: 400000 },
            },
          ],
        },
      },
      {
        kind: 'tool',
        message: {
          role: 'tool',
          name: 'local_exec_shell',
          tool_call_id: 'call_1',
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                success: true,
                data: { stdout: '2026-09-13 13:37:02\n你好\n' },
              }),
            },
          ],
        },
      },
      {
        kind: 'assistant',
        message: {
          role: 'assistant',
          content: [{ type: 'text', text: '第 1 轮完成。' }],
        },
      },
    ]);

    expect(blocks.map((b) => b.type)).toEqual(['reasoning', 'tools', 'text']);
    expect(blocks[0]).toEqual({ type: 'reasoning', content: '先睡 300 秒再问好。' });
    expect(blocks[1]).toMatchObject({
      type: 'tools',
      actions: [
        {
          id: 'call_1',
          tool: 'local_exec_shell',
          success: true,
        },
      ],
    });
    expect(blocks[2]).toEqual({ type: 'text', content: '第 1 轮完成。' });
  });

  it('pairs a tool result to the unmatched call of the same name', () => {
    const blocks = timelineFromHistoryEntries([
      {
        kind: 'assistant',
        message: {
          role: 'assistant',
          content: [],
          tool_calls: [{ id: 'a', name: 'local_exec_shell', arguments: { command: 'date' } }],
        },
      },
      {
        kind: 'tool',
        message: {
          role: 'tool',
          name: 'local_exec_shell',
          content: JSON.stringify({ success: false, error: 'tool_timeout' }),
        },
      },
    ]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: 'tools',
      actions: [{ id: 'a', tool: 'local_exec_shell', success: false, error: 'tool_timeout' }],
    });
  });
});
