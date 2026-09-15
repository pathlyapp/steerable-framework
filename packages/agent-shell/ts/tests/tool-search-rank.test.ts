import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  rankTools,
  resolveMaxResults,
  scoreTools,
  TOOL_SEARCH_DEFAULT_MAX_RESULTS,
  TOOL_SEARCH_MAX_RESULTS_CEILING,
} from '../src/tool-search-rank.js';

// `tool_search` 在桌面与 steerable-framework 各有一份实现，但对模型是同一
// 个工具：名次不同就是两个契约。三层证据，强度递减但覆盖面递增：
//
//   1. 桌面侧自身行为（本文件上半）。
//   2. vendored 契约里的分数向量——**始终执行**，孤立 CI 也守得住，是 CI 里
//      唯一真正拦住漂移的一层（`tool-contract.test.ts` 另有 vendored 与框架
//      canonical 的逐字节比对）。
//   3. 同 workspace 时直接调框架那份 BM25 比对；没有框架 checkout 就跳过。

interface ToolSearchContract {
  bm25: { k1: number; b: number; nameWeight: number };
  defaultMaxResults: number;
  maxResultsCeiling: number;
  scoreTolerance: number;
  inventory: Array<{ name: string; description: string }>;
  scoreVectors: Array<{ query: string; ranked: Array<{ name: string; score: number }> }>;
}
const CONTRACT = JSON.parse(
  readFileSync(
    path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'contracts', 'tool-contract.json'),
    'utf-8',
  ),
) as { toolSearch: ToolSearchContract };

const FRAMEWORK = process.env.STEERABLE_FRAMEWORK_DIR ?? path.resolve('../steerable-framework');
const FRAMEWORK_PY = path.join(FRAMEWORK, '.venv/bin/python');
const RANK_PY = path.join(
  FRAMEWORK,
  'packages/agent-runtime/py/src/steerable_agent_runtime/tool_search.py',
);

const INVENTORY = [
  { name: 'mcp__github__create_issue', description: 'Create a GitHub issue in a repository' },
  { name: 'mcp__github__list_prs', description: 'List pull requests on GitHub' },
  { name: 'mcp__slack__post_message', description: 'Post a message to a Slack channel' },
  { name: 'mcp__jira__create_ticket', description: 'Create a ticket, similar to an issue' },
  { name: 'mcp__linear__issue_search', description: 'Search issues across a Linear workspace' },
];

/** 用框架那份 `_rank` 给同一组名录打分，返回 [名次, 分数]。 */
function frameworkScores(query: string): Array<[string, number]> {
  const script = `
import json, sys
sys.path.insert(0, ${JSON.stringify(path.join(FRAMEWORK, 'packages/agent-runtime/py/src'))})
from steerable_agent_runtime.tool_search import _rank

class T:
    def __init__(self, name, description):
        self.name = name
        self.description = description

inventory = [T(**item) for item in json.loads(sys.argv[1])]
print(json.dumps([[tool.name, score] for score, tool in _rank(inventory, sys.argv[2])]))
`;
  const out = execFileSync(FRAMEWORK_PY, ['-c', script, JSON.stringify(INVENTORY), query], {
    encoding: 'utf8',
  });
  return JSON.parse(out.trim()) as Array<[string, number]>;
}

describe('tool_search BM25 排序', () => {
  it('只返回含该词的工具，同词命中按文档长度归一排序', () => {
    // 两者的 name 和 description 都含 'github'，BM25 的长度归一让更短的
    // 文档得分更高——框架那份实现给出同样的名次（见下方比对）。
    expect(rankTools(INVENTORY, 'github').map((t) => t.name)).toEqual([
      'mcp__github__list_prs',
      'mcp__github__create_issue',
    ]);
  });

  it('name 命中权重高于仅 description 命中', () => {
    // 'slack' 只出现在一个工具的 name 里；'ticket' 只在另一个的 name 里。
    const ranked = rankTools(INVENTORY, 'slack');
    expect(ranked[0].name).toBe('mcp__slack__post_message');
  });

  it('不含任何查询词的文档被丢弃（下限仍是过滤）', () => {
    expect(rankTools(INVENTORY, 'kubernetes')).toEqual([]);
  });

  it('多词查询按累计相关度排序，缺词不再一票否决', () => {
    const ranked = rankTools(INVENTORY, 'create issue');
    // 两词都命中的排在只命中一词的之前，但后者不被抹掉。
    expect(ranked[0].name).toBe('mcp__github__create_issue');
    expect(ranked.map((t) => t.name)).toContain('mcp__linear__issue_search');
  });

  it('工具名按限定词与动词分词，模型的提问用词能命中', () => {
    // 'mcp__jira__create_ticket' 的名字被切成 mcp/jira/create/ticket。
    expect(rankTools(INVENTORY, 'jira').map((t) => t.name)).toEqual(['mcp__jira__create_ticket']);
  });

  it('同分时按 name 升序，名次是确定的', () => {
    const tied = [
      { name: 'b_tool', description: 'identical text' },
      { name: 'a_tool', description: 'identical text' },
    ];
    expect(rankTools(tied, 'identical').map((t) => t.name)).toEqual(['a_tool', 'b_tool']);
  });

  it('空名录不报错', () => {
    expect(rankTools([], 'anything')).toEqual([]);
  });

  it('默认 8、封顶 20、下限 1', () => {
    expect(resolveMaxResults(undefined)).toBe(TOOL_SEARCH_DEFAULT_MAX_RESULTS);
    expect(resolveMaxResults(100)).toBe(TOOL_SEARCH_MAX_RESULTS_CEILING);
    expect(resolveMaxResults(0)).toBe(1);
    expect(resolveMaxResults(-5)).toBe(1);
    expect(resolveMaxResults(3)).toBe(3);
    expect(resolveMaxResults('nonsense')).toBe(TOOL_SEARCH_DEFAULT_MAX_RESULTS);
  });
});

describe('tool_search 对齐 vendored 契约', () => {
  // 契约里的分数向量由框架那份 BM25 生成（框架侧 test_tool_contract.py 反过来
  // 钉住它们仍是 Python 的真实输出）。桌面必须重现同一批分数——这是本仓 CI
  // 里唯一不依赖兄弟 checkout 的一层。
  const section = CONTRACT.toolSearch;

  it('默认上限与封顶取自契约', () => {
    expect(TOOL_SEARCH_DEFAULT_MAX_RESULTS).toBe(section.defaultMaxResults);
    expect(TOOL_SEARCH_MAX_RESULTS_CEILING).toBe(section.maxResultsCeiling);
  });

  it.each(section.scoreVectors.map((v) => [v.query, v] as const))(
    '查询 %j 重现契约里的分数与名次',
    (_query, vector) => {
      const ours = scoreTools(section.inventory, vector.query);
      expect(ours.map(({ tool }) => tool.name)).toEqual(vector.ranked.map((m) => m.name));
      ours.forEach(({ score }, i) => {
        expect(Math.abs(score - vector.ranked[i].score)).toBeLessThan(section.scoreTolerance);
      });
    },
  );

  it('契约本身带了词表外查询的空结果向量（下限是过滤，不是排序）', () => {
    // 否则一份只含命中查询的向量集会让「不含查询词就丢弃」这条无人可守。
    expect(section.scoreVectors.some((v) => v.ranked.length === 0)).toBe(true);
  });
});

describe.runIf(existsSync(FRAMEWORK_PY) && existsSync(RANK_PY))(
  'tool_search 与框架实现同名次',
  () => {
    // 比分数而非只比名次：k1／b／name 权重／idf 形式任何一项漂移都会改分数，
    // 而名次可能恰好不变（NAME_WEIGHT 2→1 对本名录就不改名次）。
    it.each([
      'github',
      'create issue',
      'message',
      'search',
      'issue repository',
      'issue',
      'create',
      'slack channel',
      'linear workspace search',
      'mcp',
    ])('查询 %j 在两侧给出同一分数与名次', (query) => {
      const ours = scoreTools(INVENTORY, query).map(
        ({ tool, score }) => [tool.name, score] as [string, number],
      );
      const theirs = frameworkScores(query);
      expect(ours.map(([name]) => name)).toEqual(theirs.map(([name]) => name));
      expect(ours.length).toBe(theirs.length);
      ours.forEach(([, score], i) => {
        expect(score).toBeCloseTo(theirs[i][1], 12);
      });
    });

    it('框架的默认上限与桌面一致', () => {
      const out = execFileSync(
        FRAMEWORK_PY,
        [
          '-c',
          `import sys; sys.path.insert(0, ${JSON.stringify(
            path.join(FRAMEWORK, 'packages/agent-runtime/py/src'),
          )});` +
            'from steerable_agent_runtime.tool_search import DEFAULT_MAX_RESULTS, MAX_RESULTS_CEILING;' +
            'print(DEFAULT_MAX_RESULTS, MAX_RESULTS_CEILING)',
        ],
        { encoding: 'utf8' },
      );
      expect(out.trim()).toBe(
        `${TOOL_SEARCH_DEFAULT_MAX_RESULTS} ${TOOL_SEARCH_MAX_RESULTS_CEILING}`,
      );
    });
  },
);
