import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  buildForcedSkillMessage,
  buildSystemPrompt,
} from '../../src/local-backend/prompt-builder.js';
import type { SkillModule } from '../../src/local-backend/skill-loader.js';

// SKILL.md 解析（frontmatter、layer 派生、条件匹配）已下沉框架
// (`steerable_agent_runtime.skills`)，桌面经 sidecar `skills.list` RPC 拿
// 解析好的模块。这里 mock skill-loader，直接构造 SkillModule 来测
// prompt-builder 的装配逻辑（eager/catalog 分层、预算裁剪、forced skill /
// MCP 工具注入）。

const mocks = vi.hoisted(() => ({
  loadSkills: vi.fn(),
  findSkill: vi.fn(),
}));

vi.mock('../../src/local-backend/skill-loader.js', () => ({
  loadSkills: mocks.loadSkills,
  findSkill: mocks.findSkill,
}));

function makeSkill(overrides: Partial<SkillModule>): SkillModule {
  return {
    name: 'skill',
    displayName: '',
    description: '',
    priority: 100,
    tags: [],
    conditions: [],
    match: 'any',
    layer: 'catalog',
    modelInvocable: true,
    content: '',
    dirName: 'skill',
    skillsDir: '/skills',
    ...overrides,
  };
}

// 基础 + 任务技能的分层共存：
//   00-base       —— eager 层（正文常驻系统提示词）
//   90-csv-tools  —— catalog 层（只进目录，按需加载）
//   aa-user-skill —— catalog 层用户技能（带 displayName）
//   zz-other-skill—— catalog 层，验证 dirName 排序
const BASE = makeSkill({
  name: 'base',
  dirName: '00-base',
  priority: 900,
  layer: 'eager',
  content: 'BASE_EAGER_CONTENT 你是本地助手',
});
const CFLOG = makeSkill({
  name: 'csv-tools',
  dirName: '90-csv-tools',
  content: 'CSV_EMBEDDED_GUIDANCE 用 csv_scan_workspace 读取目录',
});
const USER = makeSkill({
  name: 'my-workspace',
  displayName: '读工区技能',
  dirName: 'aa-user-skill',
  content: 'USER_SKILL_PROCEDURE 按我的流程从本地文件读工区',
});
const OTHER = makeSkill({
  name: 'other',
  dirName: 'zz-other-skill',
  content: 'OTHER_SKILL_CONTENT',
});

beforeEach(() => {
  mocks.loadSkills.mockReset();
  mocks.findSkill.mockReset();
  mocks.loadSkills.mockResolvedValue([BASE, CFLOG, USER, OTHER]);
});

describe('prompt-builder / A6 分层披露', () => {
  it('eagerOnly: true 时只有 eager 层技能正文进系统提示词', async () => {
    const { prompt, modules } = await buildSystemPrompt({
      ignoreConditions: true,
      eagerOnly: true,
    });
    expect(prompt).toContain('BASE_EAGER_CONTENT');
    expect(prompt).not.toContain('CSV_EMBEDDED_GUIDANCE');
    expect(prompt).not.toContain('USER_SKILL_PROCEDURE');
    expect(prompt).not.toContain('OTHER_SKILL_CONTENT');
    expect(modules.map((m) => m.dirName)).toEqual(['00-base']);
  });

  it('eagerOnly 缺省时两层都进（非 CoreLoop 路径的兼容行为），按 dirName 排序', async () => {
    const { prompt } = await buildSystemPrompt({ ignoreConditions: true });
    const basePos = prompt.indexOf('BASE_EAGER_CONTENT');
    const csvPos = prompt.indexOf('CSV_EMBEDDED_GUIDANCE');
    const userPos = prompt.indexOf('USER_SKILL_PROCEDURE');
    const otherPos = prompt.indexOf('OTHER_SKILL_CONTENT');
    for (const pos of [basePos, csvPos, userPos, otherPos]) expect(pos).toBeGreaterThan(-1);
    // localeCompare：数字前缀排字母前缀之前
    expect(basePos).toBeLessThan(csvPos);
    expect(userPos).toBeGreaterThan(csvPos);
    expect(otherPos).toBeGreaterThan(userPos);
  });

  it('eager 层技能正文进系统提示词，catalog 层不进', async () => {
    const pinned = makeSkill({
      name: 'pinned',
      dirName: 'bb-pinned',
      layer: 'eager',
      content: 'PINNED_EAGER_CONTENT',
    });
    mocks.loadSkills.mockResolvedValue([BASE, pinned, CFLOG]);
    const { prompt } = await buildSystemPrompt({
      ignoreConditions: true,
      eagerOnly: true,
    });
    expect(prompt).toContain('PINNED_EAGER_CONTENT');
    expect(prompt).not.toContain('CSV_EMBEDDED_GUIDANCE');
  });

  it('catalog 层技能即使 priority 高也不进 eagerOnly 提示词', async () => {
    const demoted = makeSkill({
      name: 'demoted',
      dirName: 'cc-demoted',
      priority: 999,
      layer: 'catalog',
      content: 'DEMOTED_CATALOG_CONTENT',
    });
    mocks.loadSkills.mockResolvedValue([BASE, demoted]);
    const { prompt } = await buildSystemPrompt({
      ignoreConditions: true,
      eagerOnly: true,
    });
    expect(prompt).not.toContain('DEMOTED_CATALOG_CONTENT');
  });
});

describe('prompt-builder / 智能体勾选的技能', () => {
  it('勾选的 catalog 层技能正文照样常驻——用户为该智能体点名了', async () => {
    const { prompt, modules } = await buildSystemPrompt({
      ignoreConditions: true,
      eagerOnly: true,
      pinnedSkillNames: ['90-csv-tools'],
    });
    expect(prompt).toContain('BASE_EAGER_CONTENT');
    expect(prompt).toContain('CSV_EMBEDDED_GUIDANCE');
    expect(prompt).not.toContain('USER_SKILL_PROCEDURE');
    expect(modules.map((m) => m.dirName)).toEqual(['00-base', '90-csv-tools']);
  });

  it('触发条件没命中的技能也能被勾选进来', async () => {
    // 条件过滤后只剩 eager 层的 00-base；勾选项从全量那次加载里补回。
    mocks.loadSkills.mockImplementation(
      async (options: { ignoreConditions?: boolean } = {}) =>
        options.ignoreConditions ? [BASE, CFLOG, USER, OTHER] : [BASE],
    );
    const { prompt } = await buildSystemPrompt({
      conditions: ['has-tools'],
      eagerOnly: true,
      pinnedSkillNames: ['my-workspace'],
    });
    expect(prompt).toContain('BASE_EAGER_CONTENT');
    expect(prompt).toContain('USER_SKILL_PROCEDURE');
    expect(prompt).not.toContain('CSV_EMBEDDED_GUIDANCE');
  });

  it('模式级排除优先于勾选：plan 模式的执行类技能不因勾选而回来', async () => {
    mocks.loadSkills.mockImplementation(
      async (options: { excludeSkillNames?: Iterable<string> } = {}) => {
        const excluded = new Set(Array.from(options.excludeSkillNames ?? []));
        return [BASE, CFLOG].filter(
          (module) => !excluded.has(module.dirName) && !excluded.has(module.name),
        );
      },
    );
    const { prompt } = await buildSystemPrompt({
      ignoreConditions: true,
      eagerOnly: true,
      excludeSkillNames: ['csv-tools'],
      pinnedSkillNames: ['90-csv-tools'],
    });
    expect(prompt).toContain('BASE_EAGER_CONTENT');
    expect(prompt).not.toContain('CSV_EMBEDDED_GUIDANCE');
  });

  it('没有勾选项时不额外取一次全量技能', async () => {
    await buildSystemPrompt({ ignoreConditions: true, eagerOnly: true });
    expect(mocks.loadSkills).toHaveBeenCalledTimes(1);
  });
});

describe('prompt-builder / 智能体自称', () => {
  const IDENTITY = makeSkill({
    name: 'identity',
    dirName: '00-identity',
    priority: 1000,
    layer: 'eager',
    content: '你是 **{agentName}**。问你是谁时回答 "{agentName}"。',
  });

  it('有 identityName 时技能正文 {agentName} 用智能体显示名', async () => {
    mocks.loadSkills.mockResolvedValue([IDENTITY]);
    const { prompt } = await buildSystemPrompt({
      eagerOnly: true,
      identityName: '电脑操作员',
    });
    expect(prompt).toContain('你是 **电脑操作员**');
    expect(prompt).toContain('回答 "电脑操作员"');
    expect(prompt).not.toContain('{agentName}');
  });

  it('没有 identityName 时 {agentName} 回落产品品牌', async () => {
    mocks.loadSkills.mockResolvedValue([IDENTITY]);
    const { prompt } = await buildSystemPrompt({ eagerOnly: true });
    expect(prompt).toContain('你是 **Agent**');
    expect(prompt).not.toContain('{agentName}');
  });

  it('没有技能时 fallback 自称也用 identityName', async () => {
    mocks.loadSkills.mockResolvedValue([]);
    const { prompt } = await buildSystemPrompt({ identityName: '电脑操作员' });
    expect(prompt).toContain('你是 电脑操作员');
  });
});

describe('prompt-builder / "/技能名" 一次性注入（buildForcedSkillMessage）', () => {
  it('渲染指令头 + 技能正文，不再经过系统提示词', async () => {
    mocks.findSkill.mockResolvedValue(USER);
    const skill = await mocks.findSkill('my-workspace');
    expect(skill).not.toBeNull();
    const message = buildForcedSkillMessage(skill!);
    const headerPos = message.indexOf('本轮指定技能（最高优先级）');
    const bodyPos = message.indexOf('USER_SKILL_PROCEDURE');
    expect(headerPos).toBeGreaterThan(-1);
    expect(bodyPos).toBeGreaterThan(headerPos); // 技能内容紧跟指令头
    // 指令头必须包含「不要改用其他工具完成本技能已覆盖任务」的降级规则
    expect(message).toContain('其他工具');
    expect(message).toContain('/读工区技能');
  });
});

describe('prompt-builder / 指定 MCP 工具', () => {
  const forcedTool = {
    token: 'mcp__demo-local__add',
    toolName: 'add',
    serverName: 'demo-local',
    description: '两个数字相加',
    available: true,
  };

  it('available=true：渲染"必须调用"指令，且位于提示词最末尾', async () => {
    const { prompt } = await buildSystemPrompt({
      ignoreConditions: true,
      forcedMcpTool: forcedTool,
    });
    const mcpPos = prompt.indexOf('本轮指定 MCP 工具（最高优先级）');
    expect(mcpPos).toBeGreaterThan(-1);
    expect(mcpPos).toBeGreaterThan(prompt.indexOf('USER_SKILL_PROCEDURE')); // 比所有技能更靠后
    expect(prompt).toContain('`mcp__demo-local__add`');
    expect(prompt).toContain('必须调用工具');
    expect(prompt).toContain('demo-local');
    expect(prompt).toContain('两个数字相加');
    expect(prompt).toContain('不得用历史/缓存数据冒充');
  });

  it('available=false：渲染"不可用"指令而非强制调用', async () => {
    const { prompt } = await buildSystemPrompt({
      ignoreConditions: true,
      forcedMcpTool: { ...forcedTool, available: false },
    });
    expect(prompt).toContain('本轮指定 MCP 工具（当前不可用）');
    expect(prompt).toContain('**不要**假装调用该工具');
    expect(prompt).toContain('设置 → MCP 服务');
    expect(prompt).not.toContain('必须调用工具');
  });

  it('在预算裁剪之后追加：charBudget 再小也不会被裁掉', async () => {
    const { prompt, droppedModules } = await buildSystemPrompt({
      ignoreConditions: true,
      charBudget: 10, // 所有技能都会被裁
      forcedMcpTool: forcedTool,
    });
    expect(droppedModules.length).toBeGreaterThan(0); // 确认预算确实裁了别的
    expect(prompt).toContain('本轮指定 MCP 工具（最高优先级）');
  });
});
