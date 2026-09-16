import { describe, expect, it } from 'vitest';
import {
  expandRunCodeActions,
  summarizeRunCodeAction,
  summarizeWebAction,
} from './executed-actions-model';

// W5-2 工具卡中文摘要的纯映射层测试：web_search 出查询词与结果计数，
// web_fetch 出短 URL、HTTP 状态、体量与截断标记；失败时只出意图头，
// 不编造结果字段。

describe('summarizeWebAction / web_search', () => {
  it('成功时出查询词与结果计数', () => {
    const summary = summarizeWebAction(
      'web_search',
      { query: 'KV cache 量化' },
      { success: true, data: { result_count: 8, query: 'KV cache 量化', results: [] } },
    );
    expect(summary).toBe('搜索“KV cache 量化” → 8 条结果');
  });

  it('长查询词截断到 40 字符', () => {
    const summary = summarizeWebAction(
      'web_search',
      { query: 'a'.repeat(60) },
      { success: true, data: { result_count: 1 } },
    );
    expect(summary).toBe(`搜索“${'a'.repeat(37)}…” → 1 条结果`);
  });

  it('失败时只出意图头，不编造计数', () => {
    const summary = summarizeWebAction(
      'web_search',
      { query: 'x' },
      { success: false, error: 'web search timed out: ReadTimeout' },
    );
    expect(summary).toBe('搜索“x”');
  });

  it('data 缺失（被截断吃掉）时退化为意图头', () => {
    expect(summarizeWebAction('web_search', { query: 'x' }, { success: true })).toBe(
      '搜索“x”',
    );
  });
});

describe('summarizeWebAction / web_fetch', () => {
  it('成功时出短 URL、状态码与体量', () => {
    const summary = summarizeWebAction(
      'web_fetch',
      { url: 'https://example.com/docs/spec.html' },
      {
        success: true,
        data: {
          url: 'https://example.com/docs/spec.html',
          status: 200,
          bytes: 12_288,
          truncated: false,
        },
      },
    );
    expect(summary).toBe('抓取 example.com/docs/spec.html → 200 · 12.0 KB');
  });

  it('截断的抓取带"已截断"标记', () => {
    const summary = summarizeWebAction(
      'web_fetch',
      { url: 'https://example.com/big' },
      { success: true, data: { status: 200, bytes: 1_000_000, truncated: true } },
    );
    expect(summary).toBe('抓取 example.com/big → 200 · 976.6 KB · 已截断');
  });

  it('失败时只出意图头（SSRF 拒绝 / 超时 / 跨域重定向）', () => {
    const summary = summarizeWebAction(
      'web_fetch',
      { url: 'http://169.254.169.254/latest' },
      { success: false, error: 'refusing to fetch a non-public address' },
    );
    expect(summary).toBe('抓取 169.254.169.254/latest');
  });

  it('无法解析的 URL 原样截断展示', () => {
    const summary = summarizeWebAction(
      'web_fetch',
      { url: 'not a url at all' },
      { success: false, error: 'unsupported scheme' },
    );
    expect(summary).toBe('抓取 not a url at all');
  });
});

describe('summarizeWebAction / 其他工具', () => {
  it('非 web 工具返回 null（调用方回落通用摘要）', () => {
    expect(
      summarizeWebAction('local_exec_shell', { command: 'ls' }, { success: true }),
    ).toBeNull();
  });
});

describe('summarizeRunCodeAction / expandRunCodeActions', () => {
  it('摘要写出描述和内层工具数，而不是整段 code', () => {
    const summary = summarizeRunCodeAction(
      'run_code',
      { code: 'return tools.call("stub_a")', description: 'two stubs' },
      {
        success: true,
        data: {
          calls: [
            { tool: 'stub_a', arguments: {}, result: { success: true } },
            { tool: 'stub_b', arguments: {}, result: { success: true } },
          ],
        },
      },
    );
    expect(summary).toBe('程序「two stubs」· 2 个内层工具');
  });

  it('展开为程序行 + 内层工具行', () => {
    const rows = expandRunCodeActions([
      {
        tool: 'run_code',
        arguments: { description: 'two stubs' },
        result: {
          success: true,
          data: {
            calls: [{ tool: 'stub_a', arguments: { x: 1 }, result: { success: true } }],
          },
        },
      },
    ]);
    expect(rows.map((r) => r.tool)).toEqual(['run_code', 'stub_a']);
  });
});
