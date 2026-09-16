import { describe, expect, it } from 'vitest';
import {
  McpServerRegistry,
  sanitizeServerKey,
  type McpServerEntry,
} from '../src/mcp-server-registry.js';

function makeMemoryStore() {
  let data: McpServerEntry[] = [];
  return {
    get: (key: 'mcpServers') => (key === 'mcpServers' ? data : undefined),
    set: (key: 'mcpServers', value: McpServerEntry[]) => {
      if (key === 'mcpServers') data = value;
    },
  };
}

function makeRegistry() {
  return new McpServerRegistry(makeMemoryStore());
}

describe('mcp-server-registry / CRUD', () => {
  it('create + list + get（按 id 和按名称）', () => {
    const registry = makeRegistry();
    const entry = registry.create({ name: 'filesystem', command: 'npx', args: ['-y', 'srv'] });
    expect(entry.enabled).toBe(true);
    expect(registry.list()).toHaveLength(1);
    expect(registry.get(entry.id)?.name).toBe('filesystem');
    expect(registry.get('FILESYSTEM')?.id).toBe(entry.id); // 名称大小写不敏感
  });

  it('拒绝空名称 / 空命令 / 重名', () => {
    const registry = makeRegistry();
    registry.create({ name: 'a', command: 'npx' });
    expect(() => registry.create({ name: '  ', command: 'npx' })).toThrow('名称不能为空');
    expect(() => registry.create({ name: 'b', command: ' ' })).toThrow('命令不能为空');
    expect(() => registry.create({ name: 'A', command: 'npx' })).toThrow('同名');
  });

  it('update 改名 / 改启用态，并检测与其他服务的重名', () => {
    const registry = makeRegistry();
    const a = registry.create({ name: 'a', command: 'npx' });
    registry.create({ name: 'b', command: 'uvx' });
    const updated = registry.update(a.id, { name: 'a2', enabled: false });
    expect(updated.name).toBe('a2');
    expect(updated.enabled).toBe(false);
    expect(() => registry.update(a.id, { name: 'B' })).toThrow('同名');
    expect(() => registry.update('nonexistent-id', { name: 'x' })).toThrow('不存在');
  });

  it('update 清空命令时报错；update 会失效工具缓存', async () => {
    const registry = makeRegistry();
    const entry = registry.create({ name: 'a', command: 'npx' });
    // 手动塞一个缓存（私有字段，测试专用）
    (registry as unknown as { toolCache: Map<string, unknown> }).toolCache.set(entry.id, {
      tools: [{ name: 't', description: '' }],
      fetchedAt: 'x',
      error: null,
    });
    expect(registry.getCachedTools(entry.id)).not.toBeNull();
    expect(() => registry.update(entry.id, { command: ' ' })).toThrow('命令不能为空');
    registry.update(entry.id, { command: 'uvx' });
    expect(registry.getCachedTools(entry.id)).toBeNull();
  });

  it('delete 移除并失效缓存；不存在的 id 返回 false', () => {
    const registry = makeRegistry();
    const entry = registry.create({ name: 'a', command: 'npx' });
    expect(registry.delete('nope')).toBe(false);
    expect(registry.delete(entry.id)).toBe(true);
    expect(registry.list()).toHaveLength(0);
  });
});

function seedToolCache(registry: McpServerRegistry, serverId: string, tools: { name: string; description?: string }[]) {
  (registry as unknown as { toolCache: Map<string, unknown> }).toolCache.set(serverId, {
    tools: tools.map((t) => ({ name: t.name, description: t.description ?? '' })),
    fetchedAt: '2026-07-31T00:00:00Z',
    error: null,
  });
}

describe('mcp-server-registry / listEnabledToolEntries + findToolByToken', () => {
  it('扁平列出所有已启用服务的工具，token 命名 mcp__<serverKey>__<toolName>', () => {
    const registry = makeRegistry();
    const demo = registry.create({ name: 'demo-local', command: 'node' });
    const fs2 = registry.create({ name: 'My FileSystem!', command: 'npx' });
    seedToolCache(registry, demo.id, [{ name: 'add', description: '加法' }, { name: 'echo' }]);
    seedToolCache(registry, fs2.id, [{ name: 'readFile' }]);

    const entries = registry.listEnabledToolEntries();
    expect(entries.map((e) => e.token).sort()).toEqual([
      'mcp__demo-local__add',
      'mcp__demo-local__echo',
      'mcp__my-filesystem__readFile',
    ]);
    const add = entries.find((e) => e.toolName === 'add')!;
    expect(add.serverName).toBe('demo-local');
    expect(add.description).toBe('加法');
    expect(add.serverId).toBe(demo.id);
  });

  it('跳过未启用服务和工具缓存为空的服务', () => {
    const registry = makeRegistry();
    const off = registry.create({ name: 'off', command: 'node', enabled: false });
    const empty = registry.create({ name: 'empty', command: 'node' });
    seedToolCache(registry, off.id, [{ name: 't' }]);
    seedToolCache(registry, empty.id, []);
    expect(registry.listEnabledToolEntries()).toEqual([]);
  });

  it('findToolByToken 大小写不敏感并返回规范 token；未命中返回 null', () => {
    const registry = makeRegistry();
    const demo = registry.create({ name: 'demo-local', command: 'node' });
    seedToolCache(registry, demo.id, [{ name: 'readFile' }]);

    const hit = registry.findToolByToken('MCP__DEMO-LOCAL__READFILE');
    expect(hit?.token).toBe('mcp__demo-local__readFile'); // 保留工具名原始大小写
    expect(hit?.serverName).toBe('demo-local');

    expect(registry.findToolByToken('mcp__demo-local__nope')).toBeNull();
    expect(registry.findToolByToken('not-mcp-token')).toBeNull();

    // 停用服务后不可再命中（"不可用"指令路径依赖这一点）
    registry.update(demo.id, { enabled: false });
    expect(registry.findToolByToken('mcp__demo-local__readFile')).toBeNull();
  });
});

describe('mcp-server-registry / importClaudeConfig', () => {
  it('导入标准 Claude Desktop 格式（含 env 字符串化）', () => {
    const registry = makeRegistry();
    const result = registry.importClaudeConfig(
      JSON.stringify({
        mcpServers: {
          filesystem: {
            command: 'npx',
            args: ['-y', '@modelcontextprotocol/server-filesystem', 'C:/'],
            env: { TOKEN: 123, MODE: 'rw' },
          },
          everything: { command: 'uvx', args: ['mcp-everything'] },
        },
      }),
    );
    expect(result.added).toHaveLength(2);
    expect(result.skipped).toEqual([]);
    const fs = registry.get('filesystem');
    expect(fs?.command).toBe('npx');
    expect(fs?.args).toEqual(['-y', '@modelcontextprotocol/server-filesystem', 'C:/']);
    expect(fs?.env).toEqual({ TOKEN: '123', MODE: 'rw' });
  });

  it('兼容不带 mcpServers 包裹的裸对象', () => {
    const registry = makeRegistry();
    const result = registry.importClaudeConfig({
      fetch: { command: 'uvx', args: ['mcp-server-fetch'] },
    } as Record<string, unknown>);
    expect(result.added).toHaveLength(1);
    expect(registry.get('fetch')?.command).toBe('uvx');
  });

  it('重名和缺 command 的条目被跳过', () => {
    const registry = makeRegistry();
    registry.create({ name: 'a', command: 'npx' });
    const result = registry.importClaudeConfig({
      mcpServers: {
        a: { command: 'uvx' }, // 重名 → 跳过
        bad: { args: ['x'] }, // 缺 command → 跳过
        good: { command: 'node', args: ['srv.js'] },
      },
    } as Record<string, unknown>);
    expect(result.added.map((s) => s.name)).toEqual(['good']);
    expect(result.skipped).toEqual(['a', 'bad']);
    // 旧条目未被覆盖
    expect(registry.get('a')?.command).toBe('npx');
  });

  it('非法输入直接抛错', () => {
    const registry = makeRegistry();
    expect(() => registry.importClaudeConfig('{not json')).toThrow();
    expect(() => registry.importClaudeConfig('[1,2]')).toThrow('对象');
    expect(() => registry.importClaudeConfig('{"mcpServers": [1]}')).toThrow('对象');
  });
});

describe('mcp-server-registry / serverKey', () => {
  it('sanitizeServerKey：ASCII 名称清洗，中文名退化为 srv-<id6>', () => {
    expect(sanitizeServerKey('My Filesystem!', 'abcdef-1234')).toBe('my-filesystem');
    expect(sanitizeServerKey('文件系统', 'abcdef-1234')).toBe('srv-abcdef');
  });

  it('同名（清洗后撞名）服务自动追加 id 后缀', () => {
    const registry = makeRegistry();
    const a = registry.create({ name: '文件系统', command: 'npx' });
    const b = registry.create({ name: '文件助手', command: 'npx' });
    // 两个中文名都会退化成 srv-<id6>（id 不同所以 key 不同），不撞名
    expect(registry.serverKey(a)).toBe(`srv-${a.id.slice(0, 6)}`);
    expect(registry.serverKey(b)).toBe(`srv-${b.id.slice(0, 6)}`);
  });

  it('清洗后撞名时追加 id 后缀保证唯一', () => {
    const registry = makeRegistry();
    const a = registry.create({ name: 'my-tool', command: 'npx' });
    const b = registry.create({ name: 'my tool', command: 'npx' });
    // 两个名字清洗后都是 my-tool → 后者追加 id 后缀
    const keys = new Set([registry.serverKey(a), registry.serverKey(b)]);
    expect(keys.size).toBe(2);
  });
});
