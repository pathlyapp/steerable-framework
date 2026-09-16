import { describe, it, expect } from 'vitest';
import {
  estimateTokens,
  estimateMessagesTokens,
  truncateMiddle,
  deepTruncateStrings,
  compactToolResultJson,
  compactMessagesForContext,
  formatHistoryForSummary,
} from '../../src/local-backend/context-compactor.js';
import type { LlmMessage } from '../../src/llm/types.js';

describe('estimateTokens', () => {
  it('空串为 0', () => {
    expect(estimateTokens('')).toBe(0);
  });

  it('中文按 ~0.6 token/字估算', () => {
    const text = '这是一段中文测试'; // 8 个 CJK 字符
    expect(estimateTokens(text)).toBe(Math.ceil(8 * 0.6));
  });

  it('英文按 ~0.25 token/字符估算', () => {
    expect(estimateTokens('abcdefgh')).toBe(2);
  });
});

describe('truncateMiddle', () => {
  it('不超限时原样返回', () => {
    expect(truncateMiddle('short', 100)).toBe('short');
  });

  it('超限时保留头尾并插入截断标记', () => {
    const text = 'A'.repeat(5000) + 'TAIL-MARKER';
    const out = truncateMiddle(text, 500);
    expect(out.length).toBeLessThanOrEqual(520);
    expect(out.startsWith('AAAA')).toBe(true);
    expect(out.endsWith('TAIL-MARKER')).toBe(true);
    expect(out).toContain('已截断');
  });
});

describe('deepTruncateStrings', () => {
  it('递归截断长字符串字段', () => {
    const input = { stdout: 'x'.repeat(10000), nested: { log: 'y'.repeat(10000) }, keep: 'ok' };
    const out = deepTruncateStrings(input, 100) as Record<string, unknown>;
    expect((out.stdout as string).length).toBeLessThanOrEqual(120);
    expect(((out.nested as Record<string, unknown>).log as string).length).toBeLessThanOrEqual(120);
    expect(out.keep).toBe('ok');
  });

  it('数组超限时截断并标注省略数', () => {
    const input = Array.from({ length: 100 }, (_, i) => `item-${i}`);
    const out = deepTruncateStrings(input, 100, 10) as unknown[];
    expect(out.length).toBe(11);
    expect(String(out[10])).toContain('已省略 90 项');
  });
});

describe('compactToolResultJson', () => {
  it('短结果原样返回', () => {
    const json = JSON.stringify({ result: { success: true } });
    expect(compactToolResultJson(json)).toBe(json);
  });

  it('超长字段被截断且输出仍是合法 JSON', () => {
    const json = JSON.stringify({
      policy: { mode: 'read' },
      result: { success: true, stdout: 'z'.repeat(50000) },
    });
    const out = compactToolResultJson(json, { maxTotalChars: 2000, maxFieldChars: 500 });
    expect(out.length).toBeLessThanOrEqual(2000);
    const parsed = JSON.parse(out);
    expect(parsed.result.success).toBe(true);
    expect(parsed.result.stdout).toContain('已截断');
  });

  it('非法 JSON 走信封兜底且输出合法 JSON', () => {
    const out = compactToolResultJson('not-json'.repeat(2000), { maxTotalChars: 1000 });
    const parsed = JSON.parse(out);
    expect(parsed.truncated).toBe(true);
    expect(parsed.originalChars).toBeGreaterThan(1000);
  });

  it('保留 error / hint 等关键字段', () => {
    const json = JSON.stringify({
      result: {
        success: false,
        error: 'connection refused',
        hint: '请先启动 CIFLog',
        stdout: 'w'.repeat(30000),
      },
    });
    const out = compactToolResultJson(json, { maxTotalChars: 3000, maxFieldChars: 500 });
    const parsed = JSON.parse(out);
    expect(parsed.result.error).toBe('connection refused');
    expect(parsed.result.hint).toBe('请先启动 CIFLog');
  });
});

describe('compactMessagesForContext', () => {
  const bigToolContent = (tag: string): string =>
    JSON.stringify({ result: { success: true, status: tag, stdout: 'x'.repeat(20000) } });

  const buildMessages = (toolCount: number): LlmMessage[] => {
    const messages: LlmMessage[] = [
      { role: 'system', content: 'system prompt' },
      { role: 'user', content: '执行任务' },
    ];
    for (let i = 0; i < toolCount; i++) {
      messages.push({
        role: 'assistant',
        content: '',
        toolCalls: [{ id: `call-${i}`, name: 'local_exec_shell', arguments: {} }],
      });
      messages.push({
        role: 'tool',
        name: 'local_exec_shell',
        toolCallId: `call-${i}`,
        content: bigToolContent(`turn-${i}`),
      });
    }
    return messages;
  };

  it('预算内不做任何修改', () => {
    const messages = buildMessages(1);
    const before = JSON.stringify(messages);
    const result = compactMessagesForContext(messages, { maxContextTokens: 1_000_000 });
    expect(result.compactedCount).toBe(0);
    expect(JSON.stringify(messages)).toBe(before);
  });

  it('超预算时从最旧的 tool 消息开始压缩，保留最近 N 条', () => {
    const messages = buildMessages(8);
    const result = compactMessagesForContext(messages, {
      maxContextTokens: 100,
      keepRecentToolResults: 2,
    });
    expect(result.compactedCount).toBe(6);
    const toolMessages = messages.filter((m) => m.role === 'tool');
    // 前 6 条被压缩成关键字段摘要
    for (const msg of toolMessages.slice(0, 6)) {
      const parsed = JSON.parse(msg.content);
      expect(parsed.compacted).toBe(true);
      expect(parsed.result.status).toMatch(/^turn-/); // 关键字段保留
    }
    // 最近 2 条保持原文
    for (const msg of toolMessages.slice(6)) {
      expect(msg.content).toContain('xxxx');
      expect(msg.content.length).toBeGreaterThan(10000);
    }
  });

  it('不改变消息条数 / 顺序 / toolCallId 配对', () => {
    const messages = buildMessages(5);
    const idsBefore = messages.map((m) => `${m.role}:${m.toolCallId ?? ''}`);
    compactMessagesForContext(messages, { maxContextTokens: 100 });
    const idsAfter = messages.map((m) => `${m.role}:${m.toolCallId ?? ''}`);
    expect(idsAfter).toEqual(idsBefore);
  });
});

describe('formatHistoryForSummary', () => {
  it('拼装角色标签并截断超长消息', () => {
    const out = formatHistoryForSummary(
      [
        { role: 'user', content: '帮我跑 GR 卡片' },
        { role: 'assistant', content: 'k'.repeat(5000) },
      ],
      500,
    );
    expect(out).toContain('用户：帮我跑 GR 卡片');
    expect(out).toContain('助手：');
    expect(out).toContain('已截断');
    expect(out.length).toBeLessThan(1000);
  });
});

describe('estimateMessagesTokens', () => {
  it('累计 content + toolCalls 参数', () => {
    const messages: LlmMessage[] = [
      { role: 'user', content: 'hello world' },
      {
        role: 'assistant',
        content: '',
        toolCalls: [{ id: '1', name: 'tool_a', arguments: { key: 'value'.repeat(100) } }],
      },
    ];
    const total = estimateMessagesTokens(messages);
    expect(total).toBeGreaterThan(estimateTokens('hello world'));
  });
});
