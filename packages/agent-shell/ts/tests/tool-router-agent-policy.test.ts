import { describe, expect, it } from 'vitest';
import { McpServerRegistry, type McpServerEntry } from '../src/mcp-server-registry.js';
import { ToolRouter, TOOL_SEARCH_TOOL_NAME } from '../src/tool-router.js';
import { normalizeToolPolicy } from '../src/local-backend/agent-capability.js';

// 智能体工具策略在 ToolRouter 的两处执行点：广告层（listModelSchemas /
// tool_search 的发现结果）与分发层（execute 复检）。广告层挡住的工具如果
// 能从 tool_search 或直调漏回去，「限制」就只是提示词里的措辞。

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

function makeRegistryWithTools(): McpServerRegistry {
  let data: McpServerEntry[] = [];
  const registry = new McpServerRegistry({
    get: (key: 'mcpServers') => (key === 'mcpServers' ? data : undefined),
    set: (key: 'mcpServers', value: McpServerEntry[]) => {
      if (key === 'mcpServers') data = value;
    },
  });
  // 命令必须不存在：真实命令会被 execute 真的 spawn（测试会挂起）。
  const server = registry.create({
    name: 'github',
    command: 'definitely-not-a-real-command-xyz',
  });
  (registry as unknown as { toolCache: Map<string, unknown> }).toolCache.set(server.id, {
    tools: [
      { name: 'create_issue', description: 'Create a GitHub issue' },
      { name: 'list_prs', description: 'List open pull requests' },
    ],
    fetchedAt: new Date().toISOString(),
    error: null,
  });
  return registry;
}

describe('智能体工具策略 / 广告层', () => {
  it('allowlist：只有勾选的工具进模型可见列表', () => {
    const router = makeToolRouter();
    const names = router
      .listModelSchemas(
        normalizeToolPolicy({ mode: 'allowlist', tools: ['local_read_file'] }),
      )
      .map((s) => s.name);
    expect(names).toEqual(['local_read_file']);
  });

  it('denylist：勾选的工具从列表消失，其余照常', () => {
    const router = makeToolRouter();
    const names = router
      .listModelSchemas(
        normalizeToolPolicy({ mode: 'denylist', tools: ['local_exec_shell'] }),
      )
      .map((s) => s.name);
    expect(names).not.toContain('local_exec_shell');
    expect(names).toContain('local_read_file');
  });

  it('策略拒掉全部 deferred 工具时 tool_search 一并退场（没有可发现的东西）', () => {
    const router = makeToolRouter(makeRegistryWithTools());
    expect(router.listModelSchemas().map((s) => s.name)).toContain(TOOL_SEARCH_TOOL_NAME);
    const restricted = router
      .listModelSchemas(
        normalizeToolPolicy({ mode: 'allowlist', tools: ['local_read_file'] }),
      )
      .map((s) => s.name);
    expect(restricted).not.toContain(TOOL_SEARCH_TOOL_NAME);
  });

  it('缺省不限制，与未接策略前的列表一致', () => {
    const router = makeToolRouter(makeRegistryWithTools());
    expect(router.listModelSchemas(null)).toEqual(router.listModelSchemas());
  });
});

describe('智能体工具策略 / tool_search 发现缝', () => {
  it('被策略拒绝的 deferred 工具搜不出来', async () => {
    const router = makeToolRouter(makeRegistryWithTools());
    const result = (await router.execute(
      { name: TOOL_SEARCH_TOOL_NAME, arguments: { query: 'github' } },
      {
        toolPolicy: normalizeToolPolicy({
          mode: 'denylist',
          tools: ['mcp__github__create_issue'],
        }),
      },
    )) as { matches: Array<{ name: string }>; deferredCount: number };
    expect(result.matches.map((m) => m.name)).toEqual(['mcp__github__list_prs']);
    expect(result.deferredCount).toBe(1);
  });
});

describe('智能体工具策略 / 分发层复检', () => {
  it('拒绝直调被策略挡住的工具（模型从历史里捞名字也不行）', async () => {
    const router = makeToolRouter();
    await expect(
      router.execute(
        { name: 'local_exec_shell', arguments: { command: 'echo hi' } },
        {
          toolPolicy: normalizeToolPolicy({
            mode: 'denylist',
            tools: ['local_exec_shell'],
          }),
        },
      ),
    ).rejects.toThrow(/local_exec_shell/);
  });

  it('放行的工具照常执行', async () => {
    const router = makeToolRouter();
    const out = (await router.execute(
      { name: 'local_read_file', arguments: { path: 'a.txt' } },
      {
        toolPolicy: normalizeToolPolicy({
          mode: 'allowlist',
          tools: ['local_read_file'],
        }),
      },
    )) as { success: boolean };
    expect(out.success).toBe(true);
  });
});
