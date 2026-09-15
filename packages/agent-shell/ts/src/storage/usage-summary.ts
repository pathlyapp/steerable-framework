/**
 * W6-9 用量与成本归因 —— 用量聚合的纯逻辑部分。
 *
 * `LocalStore.getUsageSummary` 负责 SQL(按 model/provider 分桶 GROUP BY),
 * 本模块负责把 SQL 行映射成 `UsageSummary`(逐桶 null-cost 处理 + 总计 reduce)。
 * 拆成纯函数是为了可测:`LocalStore` 顶层加载的 better-sqlite3 native module
 * 与 vitest 的 node 运行时二进制不兼容,无法直接实例化测试。
 *
 * 成本口径:只统计有单价的模型(framework `MODEL_PRICES`);无单价模型的
 * `cost_usd` 存 NULL,SQL `SUM` 全 NULL 得 NULL → 该桶 `costUsd = null`
 * (面板渲染 "—"),且不计入总成本。
 */

/** 一个 model(+provider) 分桶的用量聚合。 */
export interface UsageModelBucket {
  model: string;
  provider: string;
  turns: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  cachedPromptTokens: number;
  /** 该桶成本合计(USD);桶内无一条有单价时为 null(渲染为 "—")。 */
  costUsd: number | null;
}

/** 用量面板数据 —— 按 model 分桶 + 总计。 */
export interface UsageSummary {
  sinceDays: number;
  byModel: UsageModelBucket[];
  totals: {
    turns: number;
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    cachedPromptTokens: number;
    /** 所有有单价模型的成本合计(无单价模型不计入)。 */
    costUsd: number;
  };
}

/** `getUsageSummary` SQL 查询返回的原始行(snake_case 列)。 */
export interface UsageSummaryRow {
  model: string;
  provider: string;
  turns: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_prompt_tokens: number;
  cost_usd: number | null;
}

/**
 * 把 SQL 分桶行聚合成 `UsageSummary`。
 * 逐桶:`cost_usd` 为 NULL/undefined → `costUsd = null`;否则转 number。
 * 总计:token/轮次直接相加;成本只累加非 null 桶(无单价模型不摊入)。
 */
export function buildUsageSummary(rows: UsageSummaryRow[], sinceDays: number): UsageSummary {
  const byModel: UsageModelBucket[] = rows.map((row) => ({
    model: String(row.model),
    provider: String(row.provider),
    turns: Number(row.turns),
    promptTokens: Number(row.prompt_tokens),
    completionTokens: Number(row.completion_tokens),
    totalTokens: Number(row.total_tokens),
    cachedPromptTokens: Number(row.cached_prompt_tokens),
    costUsd: row.cost_usd === null || row.cost_usd === undefined ? null : Number(row.cost_usd),
  }));

  const totals = byModel.reduce(
    (acc, b) => ({
      turns: acc.turns + b.turns,
      promptTokens: acc.promptTokens + b.promptTokens,
      completionTokens: acc.completionTokens + b.completionTokens,
      totalTokens: acc.totalTokens + b.totalTokens,
      cachedPromptTokens: acc.cachedPromptTokens + b.cachedPromptTokens,
      costUsd: acc.costUsd + (b.costUsd ?? 0),
    }),
    { turns: 0, promptTokens: 0, completionTokens: 0, totalTokens: 0, cachedPromptTokens: 0, costUsd: 0 },
  );

  return { sinceDays, byModel, totals };
}
