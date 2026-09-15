import { describe, expect, it } from 'vitest';
import { ToolRouter } from '../src/tool-router.js';

// W5-2 网络读取对的宿主侧接线：schema 可用性由 sidecar 握手（tool.list）
// 决定，执行经注入的 delegate 前转；未握手/未注册时工具不出场、调用响亮
// 失败。唯一实现（SSRF/上限/超时）在 sidecar 的 web_tools.py，由框架侧
// test_web_tools.py 覆盖——这里只测宿主的 schema 门与前转。

function makeToolRouter(): ToolRouter {
  return new ToolRouter(
    {
      executeShell: async () => ({ success: true }),
      readLocalFile: async () => ({ success: true, content: '' }),
      writeLocalFile: async () => ({ success: true }),
      openLocalTarget: async () => ({ success: true }),
    } as never,
    { list: () => [], getById: () => null } as never,
  );
}

describe('tool-router / web 工具（W5-2）', () => {
  it('未握手时不出场：模型列表与完整名录都没有 web_*', () => {
    const router = makeToolRouter();
    expect(router.listSchemas().some((s) => s.name.startsWith('web_'))).toBe(false);
    expect(router.listModelSchemas().some((s) => s.name.startsWith('web_'))).toBe(false);
  });

  it('握手只给了 web_fetch 时 web_search 不出场（搜索后端未配置 ≠ 可用而坏）', () => {
    const router = makeToolRouter();
    router.setWebTools(async () => ({ success: true }), ['web_fetch']);
    const names = router.listModelSchemas().map((s) => s.name);
    expect(names).toContain('web_fetch');
    expect(names).not.toContain('web_search');
  });

  it('两个都注册时以 read 模式出场（plan 模式过滤保留 read）', () => {
    const router = makeToolRouter();
    router.setWebTools(async () => ({ success: true }), ['web_fetch', 'web_search']);
    const schemas = router.listModelSchemas().filter((s) => s.name.startsWith('web_'));
    expect(schemas.map((s) => s.name).sort()).toEqual(['web_fetch', 'web_search']);
    expect(schemas.every((s) => s.mode === 'read')).toBe(true);
    // plan 模式的过滤条件就是 mode === 'read'（router.ts turnTools）
    const planVisible = router
      .listModelSchemas()
      .filter((s) => s.mode === 'read')
      .map((s) => s.name);
    expect(planVisible).toContain('web_fetch');
    expect(planVisible).toContain('web_search');
  });

  it('执行前转到注入的 delegate，参数原样传递', async () => {
    const router = makeToolRouter();
    const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
    router.setWebTools(
      async (name, args) => {
        calls.push({ name, args });
        return { success: true, data: { result_count: 3 } };
      },
      ['web_fetch', 'web_search'],
    );
    const out = (await router.execute({
      name: 'web_search',
      arguments: { query: 'kv 缓存', max_results: 5 },
    })) as { success: boolean; data: { result_count: number } };
    expect(out.success).toBe(true);
    expect(out.data.result_count).toBe(3);
    expect(calls).toEqual([
      { name: 'web_search', args: { query: 'kv 缓存', max_results: 5 } },
    ]);
  });

  it('握手集合外的调用响亮失败（sidecar 重启漂移 / 编程错误）', async () => {
    const router = makeToolRouter();
    router.setWebTools(async () => ({ success: true }), ['web_fetch']);
    await expect(
      router.execute({ name: 'web_search', arguments: { query: 'x' } }),
    ).rejects.toThrow('web_search 当前不可用');
    await expect(
      router.execute({ name: 'web_search', arguments: { query: 'x' } }),
    ).rejects.toThrow('Tavily 钥');
  });

  it('delegate 报错原样上抛（reverse-tools 包成 ToolResult 是调用方的事）', async () => {
    const router = makeToolRouter();
    router.setWebTools(
      async () => {
        throw new Error('sidecar method tool.invoke timed out');
      },
      ['web_fetch'],
    );
    await expect(
      router.execute({ name: 'web_fetch', arguments: { url: 'https://example.com' } }),
    ).rejects.toThrow('timed out');
  });

  it('hosted search intercepts web_search and does not forward to the sidecar', async () => {
    const router = makeToolRouter();
    const forwarded: string[] = [];
    router.setWebTools(
      async (name) => {
        forwarded.push(name);
        return { success: true };
      },
      ['web_fetch', 'web_search'],
    );
    router.setHostedWebSearch(async (args) => ({
      success: true,
      data: { query: args.query, hosted: true },
    }));
    const out = (await router.execute({
      name: 'web_search',
      arguments: { query: 'kv 缓存' },
    })) as { success: boolean; data: { hosted: boolean } };
    expect(out).toEqual({ success: true, data: { query: 'kv 缓存', hosted: true } });
    expect(forwarded).toEqual([]);
  });
});
