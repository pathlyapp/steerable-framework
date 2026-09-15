import { describe, expect, it } from 'vitest';
import { McpServerRegistry, type McpServerEntry } from '../src/mcp-server-registry.js';
import {
  ToolRouter,
  MCP_DYNAMIC_TOOL_PREFIX,
  TOOL_SEARCH_TOOL_NAME,
  type ToolSchema,
} from '../src/tool-router.js';
import { TOOL_SEARCH_DEFAULT_MAX_RESULTS } from '../src/tool-search-rank.js';

// Wave 2 工具分层的宿主采纳：MCP 动态工具落 deferred 层，模型可见列表
// 只出 direct 层 + tool_search 发现缝；分发不按层设卡。契约与
// steerable-framework 的 tool_search.py 一致（BM25 排序 / name 分词计两次 /
// 默认 8 封顶 20 / 返回完整 schema）。

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

function makeRegistryWithTools(): McpServerRegistry {
  const registry = makeRegistry();
  // 命令必须不存在：真实命令会被 execute 真的 spawn（测试会挂起）。
  const server = registry.create({ name: 'github', command: 'definitely-not-a-real-command-xyz' });
  seedToolCache(registry, server.id, [
    {
      name: 'create_issue',
      description: 'Create a GitHub issue',
      inputSchema: { type: 'object', properties: { title: { type: 'string' } } },
    },
    { name: 'list_prs', description: 'List open pull requests' },
  ]);
  return registry;
}

describe('tool-router / 曝光分层', () => {
  it('MCP 动态工具在完整名录里是 deferred 层', () => {
    const router = makeToolRouter(makeRegistryWithTools());
    const dynamic = router
      .listSchemas()
      .filter((s) => s.name.startsWith(MCP_DYNAMIC_TOOL_PREFIX));
    expect(dynamic.length).toBe(2);
    expect(dynamic.every((s) => s.exposure === 'deferred')).toBe(true);
  });

  it('listModelSchemas 只出 direct 层；有 deferred 层时附带 tool_search', () => {
    const router = makeToolRouter(makeRegistryWithTools());
    const names = router.listModelSchemas().map((s) => s.name);
    expect(names).toContain('local_exec_shell');
    expect(names).toContain(TOOL_SEARCH_TOOL_NAME);
    expect(names.some((n) => n.startsWith(MCP_DYNAMIC_TOOL_PREFIX))).toBe(false);
  });

  it('local_run_snippet 保持 direct 曝光（R14 P1 重命名后与 run_code 区分）', () => {
    const router = makeToolRouter(makeRegistry());
    // R14 P1 同名物理清理：local_run_code 更名为 local_run_snippet，与 sidecar
    // 的 run_code（程序化工具调用）职责区分；仍为 direct，技能条件 tool:local_run_snippet 不变。
    const schema = router.getSchemaByName('local_run_snippet');
    expect(schema).not.toBeNull();
    expect(schema?.exposure ?? 'direct').toBe('direct');
    const names = router.listModelSchemas().map((s) => s.name);
    expect(names).toContain('local_run_snippet');
    // 旧名已移除，不再与 run_code 同名并列。
    expect(names).not.toContain('local_run_code');
  });

  it('hidden 层不进模型可见列表、不进搜索', async () => {
    const router = makeToolRouter(makeRegistryWithTools());
    const original = router.listSchemas.bind(router);
    const hidden: ToolSchema = {
      name: 'internal_reset',
      description: 'Reset internal state',
      inputSchema: { type: 'object', properties: {} },
      mode: 'destructive',
      exposure: 'hidden',
    };
    router.listSchemas = () => [...original(), hidden];

    const names = router.listModelSchemas().map((s) => s.name);
    expect(names).not.toContain('internal_reset');

    const result = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'reset internal' },
    })) as { matches: Array<{ name: string }> };
    expect(result.matches).toEqual([]);
  });
});

describe('tool-router / tool_search', () => {
  it('按关键词命中 deferred 工具并返回完整 schema', async () => {
    const router = makeToolRouter(makeRegistryWithTools());
    const result = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'github' },
    })) as {
      matches: Array<{ name: string; description: string; parameters: unknown }>;
      deferredCount: number;
      note: string;
    };
    // 仅 2 个 MCP 工具在 deferred 层（local_run_snippet 为 direct，不计入）。
    expect(result.deferredCount).toBe(2);
    // name 命中（github 在工具名里）排在 description 命中之前。
    expect(result.matches.map((m) => m.name)).toEqual([
      'mcp__github__create_issue',
      'mcp__github__list_prs',
    ]);
    expect(result.matches[0].parameters).toEqual({
      type: 'object',
      properties: { title: { type: 'string' } },
    });
    expect(result.note).toContain('call them by name');
  });

  it('BM25 排序：多余的词不再抹掉命中，词表外的查询才返回空', async () => {
    const router = makeToolRouter(makeRegistryWithTools());
    // 排序而非过滤：'github' 命中即得分，'nonexistent' 只是没有贡献。
    const partial = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'github nonexistent' },
    })) as { matches: Array<{ name: string }> };
    expect(partial.matches.map((m) => m.name)).toEqual([
      'mcp__github__create_issue',
      'mcp__github__list_prs',
    ]);
    // 下限仍是过滤：一个查询词都不含的文档得 0 分被丢弃。
    const none = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'nonexistent' },
    })) as { matches: unknown[]; note: string };
    expect(none.matches).toEqual([]);
    expect(none.note).toContain('No deferred tools matched');
  });

  it('max_results 截断并封顶', async () => {
    const registry = makeRegistry();
    const server = registry.create({ name: 'big', command: 'npx' });
    seedToolCache(
      registry,
      server.id,
      Array.from({ length: 30 }, (_, i) => ({
        name: `tool_${String(i).padStart(2, '0')}`,
        description: 'common keyword',
      })),
    );
    const router = makeToolRouter(registry);
    const capped = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'common', max_results: 100 },
    })) as { matches: unknown[] };
    expect(capped.matches.length).toBe(20); // 封顶 20
    const defaulted = (await router.execute({
      name: TOOL_SEARCH_TOOL_NAME,
      arguments: { query: 'common' },
    })) as { matches: unknown[] };
    expect(defaulted.matches.length).toBe(TOOL_SEARCH_DEFAULT_MAX_RESULTS); // 默认 8
  });

  it('被发现的 deferred 工具按名直调（分发不按层设卡）', async () => {
    const router = makeToolRouter(makeRegistryWithTools());
    // mcp__github__create_issue 不在模型可见列表，但 execute 照样路由
    // （命令不存在 → 返回携带该命令的启动错误，证明带对了配置）。
    const result = (await router.execute({
      name: 'mcp__github__create_issue',
      arguments: { title: 'x' },
    })) as { success: boolean; error?: string };
    expect(result.success).toBe(false);
    expect(result.error).toContain('definitely-not-a-real-command-xyz');
  });
});
