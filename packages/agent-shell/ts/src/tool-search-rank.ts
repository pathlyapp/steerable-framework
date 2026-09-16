/**
 * `tool_search` 的 BM25 排序，与 steerable-framework
 * `packages/agent-runtime/py/src/steerable_agent_runtime/tool_search.py`
 * 同一算法与同一常量：同名工具在两侧必须给出同样的名次与同样的默认上限，
 * 否则「桌面的 tool_search」和「框架的 tool_search」是两个契约不同的工具。
 */

/** 每次调用默认返回条数；与框架 `DEFAULT_MAX_RESULTS` 一致（codex 的 tool_discovery 上限）。 */
export const TOOL_SEARCH_DEFAULT_MAX_RESULTS = 8;
/** 每次调用硬上限：每条匹配都带完整 schema，无界结果就是无界上下文注入。 */
export const TOOL_SEARCH_MAX_RESULTS_CEILING = 20;

/** BM25 饱和度／长度归一常量——Lucene 默认值，与框架一致，不重调。 */
const BM25_K1 = 1.2;
const BM25_B = 0.75;
/** name 分词计两次：命中工具自己的名字比命中描述更能说明意图。 */
const NAME_WEIGHT = 2;

/** 排序输入：只需要参与打分的两个字段。 */
export interface RankableTool {
  name: string;
  description: string;
}

/**
 * 小写字母数字连续段。`mcp__github__create_issue` 会切成限定词与动词部分，
 * 这正是模型提问时的用词方式。
 */
function tokenize(text: string): string[] {
  return text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
}

function counter(tokens: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const token of tokens) counts.set(token, (counts.get(token) ?? 0) + 1);
  return counts;
}

/**
 * 按 BM25 给 `inventory` 打分并排序，最佳在前。
 *
 * 排序而非过滤，但下限仍是过滤：不含任何查询词的文档得 0 分被丢弃——
 * 对词表外的查询返回空，胜过返回一串不相关的工具。
 *
 * 分数本身是导出的，因为与框架实现的等价性要按分数比对：只比名次会漏掉
 * 常量漂移（k1／b／name 权重变了而名次恰好不变）。
 *
 * @param inventory 待打分的工具名录。
 * @param query 模型给出的查询串。
 * @returns 命中的工具及其分数，按分数降序、同分按 name 升序。
 */
export function scoreTools<T extends RankableTool>(
  inventory: readonly T[],
  query: string,
): Array<{ tool: T; score: number }> {
  const documents = inventory.map(tool => ({
    tool,
    tokens: [
      ...Array.from({ length: NAME_WEIGHT }, () => tokenize(tool.name)).flat(),
      ...tokenize(tool.description),
    ],
  }));
  if (documents.length === 0) return [];


  const docFreq = new Map<string, number>();
  for (const { tokens } of documents) {
    for (const term of new Set(tokens)) docFreq.set(term, (docFreq.get(term) ?? 0) + 1);
  }
  const nDocs = documents.length;
  const avgLen = documents.reduce((sum, { tokens }) => sum + tokens.length, 0) / nDocs;
  const queryTerms = new Set(tokenize(query));

  const scored: Array<{ score: number; tool: T }> = [];
  for (const { tool, tokens } of documents) {
    const termFreq = counter(tokens);
    let score = 0;
    for (const term of queryTerms) {
      const freq = termFreq.get(term) ?? 0;
      if (freq === 0) continue;
      const n = docFreq.get(term) ?? 0;
      const idf = Math.log(1 + (nDocs - n + 0.5) / (n + 0.5));
      score +=
        (idf * (freq * (BM25_K1 + 1))) /
        (freq + BM25_K1 * (1 - BM25_B + (BM25_B * tokens.length) / avgLen));
    }
    if (score > 0) scored.push({ score, tool });
  }
  scored.sort((a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name));
  return scored;
}

/**
 * `scoreTools` 的名次部分。
 *
 * @param inventory 待排序的工具名录。
 * @param query 模型给出的查询串。
 * @returns 命中的工具，按分数降序、同分按 name 升序。
 */
export function rankTools<T extends RankableTool>(inventory: readonly T[], query: string): T[] {
  return scoreTools(inventory, query).map(({ tool }) => tool);
}

/** 把请求的条数收进 [1, 封顶] 区间；缺省用框架的默认值。 */
export function resolveMaxResults(requested: unknown): number {
  const asked =
    typeof requested === 'number' && Number.isFinite(requested)
      ? Math.trunc(requested)
      : TOOL_SEARCH_DEFAULT_MAX_RESULTS;
  return Math.max(1, Math.min(asked, TOOL_SEARCH_MAX_RESULTS_CEILING));
}
