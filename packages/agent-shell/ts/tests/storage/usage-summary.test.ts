import { describe, expect, it } from 'vitest';
import {
  buildUsageSummary,
  type UsageSummaryRow,
} from '../../src/storage/usage-summary';

// W6-9 用量归因:getUsageSummary 的纯逻辑(SQL 行 → 面板聚合)。
// LocalStore 顶层加载 better-sqlite3 native module,与 vitest 的 node 运行时
// 二进制不兼容,无法直接实例化;故把聚合逻辑拆成纯函数在此覆盖。

function row(overrides: Partial<UsageSummaryRow>): UsageSummaryRow {
  return {
    model: 'm',
    provider: 'p',
    turns: 1,
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    cached_prompt_tokens: 0,
    cost_usd: null,
    ...overrides,
  };
}

describe('buildUsageSummary (W6-9)', () => {
  it('returns an empty summary for no rows', () => {
    const s = buildUsageSummary([], 30);
    expect(s.sinceDays).toBe(30);
    expect(s.byModel).toEqual([]);
    expect(s.totals).toEqual({
      turns: 0,
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      cachedPromptTokens: 0,
      costUsd: 0,
    });
  });

  it('maps snake_case rows to camelCase buckets', () => {
    const s = buildUsageSummary(
      [row({ model: 'deepseek-chat', provider: 'deepseek', turns: 3, prompt_tokens: 100, completion_tokens: 40, total_tokens: 140, cached_prompt_tokens: 12, cost_usd: 0.5 })],
      7,
    );
    expect(s.byModel).toEqual([
      {
        model: 'deepseek-chat',
        provider: 'deepseek',
        turns: 3,
        promptTokens: 100,
        completionTokens: 40,
        totalTokens: 140,
        cachedPromptTokens: 12,
        costUsd: 0.5,
      },
    ]);
  });

  it('keeps costUsd null for buckets whose events are all unpriced', () => {
    const s = buildUsageSummary([row({ model: 'llama3', provider: 'ollama', cost_usd: null })], 30);
    expect(s.byModel[0].costUsd).toBeNull();
  });

  it('sums token totals across buckets', () => {
    const s = buildUsageSummary(
      [
        row({ model: 'a', turns: 2, prompt_tokens: 100, completion_tokens: 50, total_tokens: 150, cached_prompt_tokens: 10, cost_usd: 0.2 }),
        row({ model: 'b', turns: 1, prompt_tokens: 300, completion_tokens: 100, total_tokens: 400, cached_prompt_tokens: 0, cost_usd: 0.8 }),
      ],
      30,
    );
    expect(s.totals.turns).toBe(3);
    expect(s.totals.promptTokens).toBe(400);
    expect(s.totals.completionTokens).toBe(150);
    expect(s.totals.totalTokens).toBe(550);
    expect(s.totals.cachedPromptTokens).toBe(10);
  });

  it('excludes unpriced buckets from the total cost but keeps their tokens', () => {
    const s = buildUsageSummary(
      [
        row({ model: 'priced', total_tokens: 100, cost_usd: 0.5 }),
        row({ model: 'local', total_tokens: 999, cost_usd: null }),
      ],
      30,
    );
    // 成本只算有单价的;token 照常全计。
    expect(s.totals.costUsd).toBeCloseTo(0.5, 10);
    expect(s.totals.totalTokens).toBe(1099);
  });

  it('accumulates fractional costs without null-skipping priced buckets', () => {
    const s = buildUsageSummary(
      [row({ model: 'a', cost_usd: 0.001 }), row({ model: 'b', cost_usd: 0.002 })],
      30,
    );
    expect(s.totals.costUsd).toBeCloseTo(0.003, 10);
  });
});
