import { describe, expect, it } from 'vitest';
import { McpServerRegistry, type McpServerEntry } from '../src/mcp-server-registry.js';
import { ToolRouter, MCP_DYNAMIC_TOOL_PREFIX } from '../src/tool-router.js';

// ToolRouter 的 LocalExecutor / LocalScriptRegistry 用桩代替（与
// tests/cflog/tool-router.integration.test.ts 同款做法）。
function makeToolRouter(registry?: McpServerRegistry): ToolRouter {
  return new ToolRouter(
    {
      executeShell: async () => ({ success: true }),
      readLocalFile: async () => ({ success: true, content: '' }),
      writeLocalFile: async () => ({ success: true }),
      openLocalTarget: async () => ({ success: true }),
    } as never,
    { list: () => [], getById: () => null } as never,
    undefined,
    registry,
  );
}

function makeRegistry(): McpServerRegistry {
  let data: McpServerEntry[] = [];
  return new McpServerRegistry({
    get: (key: 'mcpServers') => (key === 'mcpServers' ? data : undefined),
    set: (key: 'mcpServers', value: McpServerEntry[]) => {
      if (key === 'mcpServers') data = value;
    },
  });
}

/** 私有缓存注入（测试专用）：模拟一次成功的 listTools。 */
function seedToolCache(
  registry: McpServerRegistry,
  serverId: string,
  tools: Array<{ name: string; description: string; inputSchema?: unknown }>,
): void {
  (registry as unknown as { toolCache: Map<string, unknown> }).toolCache.set(serverId, {
    tools,
    fetchedAt: new Date().toISOString(),
    error: null,
  });
}

describe('tool-router / 注册的 MCP 动态工具', () => {
  it('已启用且有缓存工具的服务，其工具以 mcp__key__tool 一等工具出场', () => {
    const registry = makeRegistry();
    const server = registry.create({ name: 'filesystem', command: 'npx' });
    seedToolCache(registry, server.id, [
      {
        name: 'read_file',
        description: 'Read one file',
        inputSchema: { type: 'object', properties: { path: { type: 'string' } } },
      },
      { name: 'list_dir', description: '' },
    ]);
    const router = makeToolRouter(registry);
    const schemas = router.listSchemas();
    const dynamic = schemas.filter((s) => s.name.startsWith(MCP_DYNAMIC_TOOL_PREFIX));
    expect(dynamic.map((s) => s.name)).toEqual([
      'mcp__filesystem__read_file',
      'mcp__filesystem__list_dir',
    ]);
    expect(dynamic[0].description).toBe('[MCP:filesystem] Read one file');
    expect(dynamic[0].mode).toBe('external');
    expect(dynamic[0].inputSchema).toEqual({
      type: 'object',
      properties: { path: { type: 'string' } },
    });
    // 缺 inputSchema 时给兜底空对象 schema
    expect(dynamic[1].inputSchema).toEqual({ type: 'object', properties: {} });
    // getSchemaByName 也能找到（plan 模式防御检查依赖它）
    expect(router.getSchemaByName('mcp__filesystem__read_file')).not.toBeNull();
  });

  it('已停用或无工具缓存的服务不出场', () => {
    const registry = makeRegistry();
    const off = registry.create({ name: 'off-srv', command: 'npx', enabled: false });
    seedToolCache(registry, off.id, [{ name: 'x', description: '' }]);
    registry.create({ name: 'no-cache', command: 'npx' });
    const dynamic = makeToolRouter(registry)
      .listSchemas()
      .filter((s) => s.name.startsWith(MCP_DYNAMIC_TOOL_PREFIX));
    expect(dynamic).toEqual([]);
  });

  it('execute 路由到 mcpExecutor：命令不存在时返回其启动错误（证明带对了配置）', async () => {
    const registry = makeRegistry();
    const server = registry.create({
      name: 'filesystem',
      command: 'definitely-not-a-real-command-xyz',
    });
    seedToolCache(registry, server.id, [{ name: 'read_file', description: '' }]);
    const router = makeToolRouter(registry);
    const result = (await router.execute({
      name: 'mcp__filesystem__read_file',
      arguments: { path: 'C:/x' },
    })) as { success: boolean; error?: string };
    expect(result.success).toBe(false);
    expect(result.error).toContain('definitely-not-a-real-command-xyz');
  });

  it('execute：未知 serverKey / 已停用服务返回引导性错误', async () => {
    const registry = makeRegistry();
    const off = registry.create({ name: 'off-srv', command: 'npx', enabled: false });
    seedToolCache(registry, off.id, [{ name: 'x', description: '' }]);
    const router = makeToolRouter(registry);
    const unknown = (await router.execute({
      name: 'mcp__ghost__do_thing',
      arguments: {},
    })) as { success: boolean; error?: string };
    expect(unknown.success).toBe(false);
    expect(unknown.error).toContain('不存在或已禁用');
    const disabled = (await router.execute({
      name: 'mcp__off-srv__x',
      arguments: {},
    })) as { success: boolean; error?: string };
    expect(disabled.success).toBe(false);
    expect(disabled.error).toContain('不存在或已禁用');
  });

  it('execute：无法解析的工具名返回明确错误', async () => {
    const router = makeToolRouter(makeRegistry());
    const result = (await router.execute({
      name: 'mcp__no-separator',
      arguments: {},
    })) as { success: boolean; error?: string };
    expect(result.success).toBe(false);
    expect(result.error).toContain('无法解析');
  });

  it('未注入注册表时：无动态工具，动态名调用返回注册表不可用', async () => {
    const router = makeToolRouter(undefined);
    expect(router.listSchemas().filter((s) => s.name.startsWith(MCP_DYNAMIC_TOOL_PREFIX))).toEqual([]);
    const result = (await router.execute({
      name: 'mcp__a__b',
      arguments: {},
    })) as { success: boolean; error?: string };
    expect(result.success).toBe(false);
    expect(result.error).toContain('注册表不可用');
  });
});
