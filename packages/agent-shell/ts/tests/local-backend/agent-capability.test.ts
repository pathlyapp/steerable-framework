import { describe, expect, it } from 'vitest';
import {
  UNRESTRICTED_CAPABILITY,
  filterToolsByPolicy,
  isSkillPinned,
  isToolAllowed,
  mergeAgentCapabilities,
  normalizeToolPolicy,
  resolveSkillExcludes,
  type AgentCapabilityInput,
  type SkillIdentity,
} from '../../src/local-backend/agent-capability.js';

// 智能体的技能勾选与工具策略解析。这份结果同时喂给提示词注入、sidecar
// 技能目录、以及工具面（每轮列表 / tool_search / 分发复检），所以这里测的
// 是「限制到底成立不成立」，而不是某个字段长什么样。

function makeAgent(overrides: Partial<AgentCapabilityInput> = {}): AgentCapabilityInput {
  return {
    skillIds: [],
    allowExternalSkills: true,
    loadAllSkills: false,
    toolPolicy: { mode: 'all', tools: [] },
    ...overrides,
  };
}

function makeSkill(dirName: string, name = dirName, displayName = ''): SkillIdentity {
  return { dirName, name, displayName };
}

describe('normalizeToolPolicy', () => {
  it('无法识别的输入归一为不限制', () => {
    expect(normalizeToolPolicy(undefined)).toEqual({ mode: 'all', tools: [] });
    expect(normalizeToolPolicy({ mode: 'nonsense', tools: 'x' })).toEqual({
      mode: 'all',
      tools: [],
    });
  });

  it('空工具集降级为不限制——「只允许零个工具」不是有效配置', () => {
    expect(normalizeToolPolicy({ mode: 'allowlist', tools: [] })).toEqual({
      mode: 'all',
      tools: [],
    });
    expect(normalizeToolPolicy({ mode: 'denylist', tools: [] })).toEqual({
      mode: 'all',
      tools: [],
    });
  });

  it('去重并丢掉空白项', () => {
    expect(
      normalizeToolPolicy({
        mode: 'allowlist',
        tools: ['local_read_file', ' local_read_file ', '', '  '],
      }),
    ).toEqual({ mode: 'allowlist', tools: ['local_read_file'] });
  });
});

describe('isToolAllowed / filterToolsByPolicy', () => {
  const schemas = [
    { name: 'local_read_file' },
    { name: 'local_exec_shell' },
    { name: 'web_search' },
  ];

  it('allowlist 只放行勾选项', () => {
    const policy = normalizeToolPolicy({
      mode: 'allowlist',
      tools: ['local_read_file'],
    });
    expect(isToolAllowed(policy, 'local_read_file')).toBe(true);
    expect(isToolAllowed(policy, 'local_exec_shell')).toBe(false);
    expect(filterToolsByPolicy(schemas, policy)).toEqual([{ name: 'local_read_file' }]);
  });

  it('denylist 只拦勾选项', () => {
    const policy = normalizeToolPolicy({
      mode: 'denylist',
      tools: ['local_exec_shell'],
    });
    expect(isToolAllowed(policy, 'local_exec_shell')).toBe(false);
    expect(filterToolsByPolicy(schemas, policy)).toEqual([
      { name: 'local_read_file' },
      { name: 'web_search' },
    ]);
  });

  it('不限制时原样返回', () => {
    expect(filterToolsByPolicy(schemas, { mode: 'all', tools: [] })).toEqual(schemas);
  });
});

describe('mergeAgentCapabilities', () => {
  it('没有绑定智能体的回合不受限制', () => {
    expect(mergeAgentCapabilities([])).toEqual(UNRESTRICTED_CAPABILITY);
  });

  it('单个智能体的配置原样生效', () => {
    expect(
      mergeAgentCapabilities([
        makeAgent({
          skillIds: ['90-csv-tools'],
          allowExternalSkills: false,
          loadAllSkills: true,
          toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] },
        }),
      ]),
    ).toEqual({
      pinnedSkills: ['90-csv-tools'],
      allowExternalSkills: false,
      loadAllSkills: true,
      toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] },
    });
  });

  it('@提及多个智能体时取最宽松——叠加专家不该反而丢能力', () => {
    expect(
      mergeAgentCapabilities([
        makeAgent({ skillIds: ['90-csv-tools'], allowExternalSkills: false }),
        makeAgent({ skillIds: ['aa-extra'], allowExternalSkills: true, loadAllSkills: true }),
      ]),
    ).toEqual({
      pinnedSkills: ['90-csv-tools', 'aa-extra'],
      allowExternalSkills: true,
      loadAllSkills: true,
      toolPolicy: { mode: 'all', tools: [] },
    });
  });

  it('两个 allowlist 合并为并集', () => {
    const merged = mergeAgentCapabilities([
      makeAgent({ toolPolicy: { mode: 'allowlist', tools: ['local_read_file'] } }),
      makeAgent({ toolPolicy: { mode: 'allowlist', tools: ['web_search'] } }),
    ]);
    expect(merged.toolPolicy).toEqual({
      mode: 'allowlist',
      tools: ['local_read_file', 'web_search'],
    });
  });

  it('两个 denylist 合并为交集——只有都拒的才拒', () => {
    const merged = mergeAgentCapabilities([
      makeAgent({
        toolPolicy: { mode: 'denylist', tools: ['local_exec_shell', 'web_search'] },
      }),
      makeAgent({ toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] } }),
    ]);
    expect(merged.toolPolicy).toEqual({
      mode: 'denylist',
      tools: ['local_exec_shell'],
    });
  });

  it('allowlist 与 denylist 混合：被另一方放行的工具不再算拒绝', () => {
    const merged = mergeAgentCapabilities([
      makeAgent({ toolPolicy: { mode: 'allowlist', tools: ['local_exec_shell'] } }),
      makeAgent({
        toolPolicy: { mode: 'denylist', tools: ['local_exec_shell', 'web_search'] },
      }),
    ]);
    expect(merged.toolPolicy).toEqual({ mode: 'denylist', tools: ['web_search'] });
    expect(isToolAllowed(merged.toolPolicy, 'local_exec_shell')).toBe(true);
    expect(isToolAllowed(merged.toolPolicy, 'web_search')).toBe(false);
  });

  it('任一智能体不限制工具则整轮不限制', () => {
    const merged = mergeAgentCapabilities([
      makeAgent({ toolPolicy: { mode: 'allowlist', tools: ['local_read_file'] } }),
      makeAgent(),
    ]);
    expect(merged.toolPolicy).toEqual({ mode: 'all', tools: [] });
  });
});

describe('isSkillPinned', () => {
  it('匹配 dirName / name / displayName，忽略大小写', () => {
    const skill = makeSkill('aa-user-skill', 'my-workspace', '读工区技能');
    expect(isSkillPinned(skill, ['aa-user-skill'])).toBe(true);
    expect(isSkillPinned(skill, ['MY-WORKSPACE'])).toBe(true);
    expect(isSkillPinned(skill, ['读工区技能'])).toBe(true);
    expect(isSkillPinned(skill, ['other'])).toBe(false);
  });

  it('空别名不误命中没有 displayName 的技能', () => {
    expect(isSkillPinned(makeSkill('00-base', 'base'), ['', '  '])).toBe(false);
  });
});

describe('resolveSkillExcludes', () => {
  const all = [
    makeSkill('00-base', 'base'),
    makeSkill('90-csv-tools', 'csv-tools'),
    makeSkill('aa-extra', 'extra'),
  ];

  it('允许其他技能时只有模式级排除生效', () => {
    const capability = mergeAgentCapabilities([makeAgent({ skillIds: ['90-csv-tools'] })]);
    expect(resolveSkillExcludes(capability, all, ['plan-mode'])).toEqual(['plan-mode']);
  });

  it('关掉「允许其他技能」后未勾选的技能全部进排除清单', () => {
    const capability = mergeAgentCapabilities([
      makeAgent({ skillIds: ['90-csv-tools'], allowExternalSkills: false }),
    ]);
    expect(resolveSkillExcludes(capability, all, ['plan-mode']).sort()).toEqual([
      '00-base',
      'aa-extra',
      'plan-mode',
    ]);
  });

  it('白名单不会盖掉模式级排除——plan 模式的执行类技能仍然出局', () => {
    const capability = mergeAgentCapabilities([
      makeAgent({ skillIds: ['85-local-exec'], allowExternalSkills: false }),
    ]);
    const excludes = resolveSkillExcludes(
      capability,
      [...all, makeSkill('85-local-exec', 'local-exec')],
      ['local-exec'],
    );
    expect(excludes).toContain('local-exec');
  });
});
