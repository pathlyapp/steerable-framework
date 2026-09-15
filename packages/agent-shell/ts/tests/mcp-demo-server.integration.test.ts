import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';
import { mcpExecutor } from '../src/mcp-executor';

/**
 * 端到端集成测试：走与线上完全相同的 mcpExecutor 代码路径
 * （StdioClientTransport 拉起子进程 → initialize 握手 → listTools → callTool），
 * 目标服务是仓库内置的零依赖 demo server（scripts/demo-mcp-server.mjs），
 * 全程本地、无网络下载，不受 npx 冷启动超时影响。
 */
const DEMO_SERVER = fileURLToPath(new URL('../scripts/demo-mcp-server.mjs', import.meta.url));
const CONFIG = { command: 'node', args: [DEMO_SERVER] };

afterAll(async () => {
  await mcpExecutor.shutdownAll();
});

describe('demo MCP server 全链路（mcpExecutor 真实路径）', () => {
  it('listTools 返回 demo 服务的 4 个工具', async () => {
    const res = await mcpExecutor.listTools(CONFIG);
    expect(res.success).toBe(true);
    const names = (res.tools ?? []).map((t) => t.name).sort();
    expect(names).toEqual(['add', 'echo', 'get_current_time', 'get_server_info']);
  });

  it('callTool echo 原样回显', async () => {
    const res = await mcpExecutor.executeTool(CONFIG, 'echo', { message: 'hello-mcp' });
    expect(res.success).toBe(true);
    expect(res.text).toBe('Echo: hello-mcp');
  });

  it('callTool add 返回两数之和', async () => {
    const res = await mcpExecutor.executeTool(CONFIG, 'add', { a: 2, b: 3 });
    expect(res.success).toBe(true);
    expect(res.text).toBe('5');
  });

  it('未知工具返回错误而不是崩溃', async () => {
    const res = await mcpExecutor.executeTool(CONFIG, 'bogus', {});
    expect(res.success).toBe(false);
    // isError 结果由 mcpExecutor 放在 error 字段（而非 text）
    expect(res.error).toContain('未知工具');
  });
});
