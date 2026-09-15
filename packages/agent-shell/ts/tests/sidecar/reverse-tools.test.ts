import { describe, expect, it, vi } from 'vitest';
import { createToolInvokeHandler } from '../../src/sidecar/reverse-tools.js';
import type { ToolRouter } from '../../src/tool-router.js';

function makeToolRouter(result: unknown = 'ok'): ToolRouter {
  return {
    execute: vi.fn(async () => result),
    listSchemas: () => [
      { name: 'local_read_file', description: '', inputSchema: {}, mode: 'read' },
      { name: 'local_write_file', description: '', inputSchema: {}, mode: 'write' },
      { name: 'local_exec_shell', description: '', inputSchema: {}, mode: 'write' },
    ],
  } as unknown as ToolRouter;
}

describe('createToolInvokeHandler', () => {
  it('executes the tool and wraps a raw result', async () => {
    const toolRouter = makeToolRouter({ stdout: 'file.txt' });
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_read_file',
      arguments: { path: '/tmp/a' },
    })) as { success: boolean; data: { value: unknown } };

    expect(toolRouter.execute).toHaveBeenCalledWith(
      {
        name: 'local_read_file',
        arguments: { path: '/tmp/a' },
      },
      undefined,
    );
    expect(out.success).toBe(true);
    expect(out.data.value).toEqual({ stdout: 'file.txt' });
  });

  it('把智能体工具策略透传进调用上下文（分发层据此复检）', async () => {
    const toolRouter = makeToolRouter('ok');
    const handler = createToolInvokeHandler({ toolRouter });

    await handler({
      name: 'local_read_file',
      arguments: { path: 'a.txt' },
      context: {
        mode: 'agent',
        chatId: 'chat-1',
        toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] },
      },
    });

    expect(toolRouter.execute).toHaveBeenCalledWith(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      {
        projectRoot: null,
        chatId: 'chat-1',
        toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] },
      },
    );
  });

  it('策略归一化后为不限制时不进调用上下文', async () => {
    const toolRouter = makeToolRouter('ok');
    const handler = createToolInvokeHandler({ toolRouter });

    // 空工具集的 allowlist 归一为「不限制」，不该在上下文里留下痕迹。
    await handler({
      name: 'local_read_file',
      arguments: { path: 'a.txt' },
      context: { mode: 'agent', chatId: 'chat-1', toolPolicy: { mode: 'allowlist', tools: [] } },
    });

    expect(toolRouter.execute).toHaveBeenCalledWith(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      { projectRoot: null, chatId: 'chat-1' },
    );
  });

  it('resolves the project root from context.chatId (W4-2 fence wiring)', async () => {
    const toolRouter = makeToolRouter('ok');
    const handler = createToolInvokeHandler({
      toolRouter,
      resolveProjectRoot: (chatId) =>
        chatId === 'chat-9' ? '/repo/project' : null,
    });

    await handler({
      name: 'local_read_file',
      arguments: { path: 'a.txt' },
      context: { mode: 'agent', chatId: 'chat-9' },
    });

    // 4.6a 起 chatId 随上下文透传（task_*/worktree_* 工具按它归组）。
    expect(toolRouter.execute).toHaveBeenCalledWith(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      { projectRoot: '/repo/project', chatId: 'chat-9' },
    );
  });

  it('explicit workspaceRoot wins over the chat-bound project root (4.6b worktree fence)', async () => {
    const toolRouter = makeToolRouter('ok');
    const handler = createToolInvokeHandler({
      toolRouter,
      resolveProjectRoot: () => '/repo/project',
    });

    await handler({
      name: 'local_read_file',
      arguments: { path: 'a.txt' },
      context: {
        mode: 'agent',
        chatId: 'chat-9',
        workspaceRoot: '/repo/project/.steerable/worktrees/demo',
      },
    });

    // 围栏根收窄到 worktree——任务摸不到主检出。
    expect(toolRouter.execute).toHaveBeenCalledWith(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      {
        projectRoot: '/repo/project/.steerable/worktrees/demo',
        chatId: 'chat-9',
      },
    );
  });

  it('passes chatId-only exec context when the chat has no project binding', async () => {
    const toolRouter = makeToolRouter('ok');
    const handler = createToolInvokeHandler({
      toolRouter,
      resolveProjectRoot: () => null,
    });

    await handler({
      name: 'local_read_file',
      arguments: { path: 'a.txt' },
      context: { mode: 'agent', chatId: 'chat-1' },
    });

    expect(toolRouter.execute).toHaveBeenCalledWith(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      { projectRoot: null, chatId: 'chat-1' },
    );
  });

  it('passes through an already-shaped ToolResult', async () => {
    const toolRouter = makeToolRouter({ success: false, error: 'Unknown tool: nope' });
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({ name: 'nope', arguments: {} })) as {
      success: boolean;
      error: string;
    };
    expect(out.success).toBe(false);
    expect(out.error).toBe('Unknown tool: nope');
  });

  it('blocks critical shell commands without executing', async () => {
    const toolRouter = makeToolRouter();
    const onBlocked = vi.fn();
    const handler = createToolInvokeHandler({ toolRouter, onBlocked });

    const out = (await handler({
      name: 'local_exec_shell',
      arguments: { command: 'sudo rm -rf /' },
    })) as { success: boolean; error: string; needsFollowup: boolean };

    expect(out.success).toBe(false);
    expect(out.error).toContain('critical');
    expect(out.needsFollowup).toBe(false);
    expect(toolRouter.execute).not.toHaveBeenCalled();
    expect(onBlocked).toHaveBeenCalledOnce();
  });

  it('allows warning-level shell commands through', async () => {
    const toolRouter = makeToolRouter('listed');
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_exec_shell',
      arguments: { command: 'ls -la' },
    })) as { success: boolean };

    expect(out.success).toBe(true);
    expect(toolRouter.execute).toHaveBeenCalledOnce();
  });

  it('maps a thrown error to a failed ToolResult', async () => {
    const toolRouter = {
      execute: vi.fn(async () => {
        throw new Error('disk on fire');
      }),
    } as unknown as ToolRouter;
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({ name: 'local_read_file', arguments: {} })) as {
      success: boolean;
      error: string;
      needsFollowup: boolean;
    };
    expect(out.success).toBe(false);
    expect(out.error).toBe('disk on fire');
    expect(out.needsFollowup).toBe(true);
  });

  it('rejects a missing tool name', async () => {
    const handler = createToolInvokeHandler({ toolRouter: makeToolRouter() });
    const out = (await handler({ arguments: {} })) as { success: boolean };
    expect(out.success).toBe(false);
  });

  it('blocks write tools in plan mode even if invoked directly', async () => {
    const toolRouter = makeToolRouter();
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_write_file',
      arguments: { path: '/tmp/a', content: 'x' },
      context: { mode: 'plan' },
    })) as { success: boolean; error: string };

    expect(out.success).toBe(false);
    expect(out.error).toContain('plan mode');
    expect(toolRouter.execute).not.toHaveBeenCalled();
  });

  it('allows read tools in plan mode', async () => {
    const toolRouter = makeToolRouter('file-contents');
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_read_file',
      arguments: { path: '/tmp/a' },
      context: { mode: 'plan' },
    })) as { success: boolean };

    expect(out.success).toBe(true);
    expect(toolRouter.execute).toHaveBeenCalledOnce();
  });

  it('plan mode hard-blocks write tools even if invoked directly', async () => {
    const toolRouter = makeToolRouter();
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_write_file',
      arguments: { path: '/tmp/x', content: 'y' },
      context: { mode: 'plan' },
    })) as { success: boolean; error: string };

    expect(out.success).toBe(false);
    expect(out.error).toContain('plan mode');
    expect(toolRouter.execute).not.toHaveBeenCalled();
  });

  it('plan mode allows read tools', async () => {
    const toolRouter = makeToolRouter('contents');
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_read_file',
      arguments: { path: '/tmp/x' },
      context: { mode: 'plan' },
    })) as { success: boolean };

    expect(out.success).toBe(true);
    expect(toolRouter.execute).toHaveBeenCalledOnce();
  });

  it('agent mode does not constrain tool choice', async () => {
    const toolRouter = makeToolRouter('done');
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_write_file',
      arguments: { path: '/tmp/x', content: 'y' },
      context: { mode: 'agent' },
    })) as { success: boolean };

    expect(out.success).toBe(true);
  });

  it('caps oversized tool results for the model context (shape preserved)', async () => {
    // 2026-08-28 E2E 实锤：local_exec_shell 对 node_modules 跑 grep -R 返回
    // ~273KB（68k token），远超框架逐项 10k token 断言线。反向通道边界必须截断。
    const toolRouter = makeToolRouter({
      success: true,
      exitCode: 0,
      stdout: 'x'.repeat(300_000),
      stderr: '',
      truncated: false,
    });
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_exec_shell',
      arguments: { command: 'grep -R foo .' },
    })) as { success: boolean; exitCode: number; stdout: string };

    expect(out.success).toBe(true);
    expect(out.exitCode).toBe(0);
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(8000);
    expect(out.stdout).toContain('已截断');
  });

  it('caps oversized raw (non-ToolResult-shaped) results too', async () => {
    const toolRouter = makeToolRouter('y'.repeat(300_000));
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_read_file',
      arguments: { path: '/tmp/big' },
    })) as { success: boolean };

    expect(out.success).toBe(true);
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(8000);
  });

  it('falls back to a truncation envelope for pathological shapes', async () => {
    // 大量中等字段：逐字段截断后仍超总量上限 → 信封兜底，输出仍是合法 JSON。
    const pathological: Record<string, string> = {};
    for (let i = 0; i < 100; i++) pathological[`field_${i}`] = 'z'.repeat(1900);
    const toolRouter = makeToolRouter({ success: true, ...pathological });
    const handler = createToolInvokeHandler({ toolRouter });

    const out = (await handler({
      name: 'local_read_file',
      arguments: { path: '/tmp/x' },
    })) as Record<string, unknown>;

    expect(JSON.stringify(out).length).toBeLessThanOrEqual(8000);
    const envelope = (out.data ?? out) as Record<string, unknown>;
    expect(envelope.truncated ?? (envelope.value as Record<string, unknown>)?.truncated).toBe(true);
  });

  it('leaves small results untouched', async () => {
    const result = { success: true, exitCode: 0, stdout: 'hello', stderr: '' };
    const toolRouter = makeToolRouter(result);
    const handler = createToolInvokeHandler({ toolRouter });

    const out = await handler({
      name: 'local_exec_shell',
      arguments: { command: 'echo hello' },
    });

    expect(out).toEqual(result);
  });
});
