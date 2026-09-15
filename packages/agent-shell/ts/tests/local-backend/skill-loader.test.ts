import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SkillModule } from '../../src/local-backend/skill-loader.js';

// SKILL.md 解析已下沉框架 (`steerable_agent_runtime.skills`)，桌面
// skill-loader 收缩为 sidecar `skills.list` RPC 的薄客户端。这些测试 mock
// supervisor，验证 RPC 参数映射、别名解析与降级（sidecar 不可用 → []）。
// 解析正确性由框架 pytest 覆盖。

const mocks = vi.hoisted(() => ({
  listSkills: vi.fn(),
  sidecarEnabled: true,
  userDataDir: '',
  // 启动竞态场景：get 返回 null 时 when 的解决值（undefined = when 也不可用）
  whenResolves: undefined as 'unset' | 'supervisor' | 'null',
}));

vi.mock('../../src/runtime.js', () => ({
  getUserDataDir: () => mocks.userDataDir,
}));

vi.mock('../../src/sidecar/handle.js', () => ({
  getSidecarSupervisor: () => (mocks.sidecarEnabled ? { listSkills: mocks.listSkills } : null),
  whenSidecarSupervisor: () =>
    mocks.whenResolves === 'supervisor'
      ? Promise.resolve({ listSkills: mocks.listSkills })
      : Promise.resolve(null),
}));

import { findSkill, getSkillsDir, listSkillRoots, loadSkills, classifySkillOrigin, setWorkspaceSkillRootsProvider } from '../../src/local-backend/skill-loader.js';

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

beforeEach(() => {
  mocks.listSkills.mockReset();
  mocks.sidecarEnabled = true;
  mocks.whenResolves = 'unset';
  mocks.userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'skill-loader-test-'));
  setWorkspaceSkillRootsProvider(null);
});

describe('skill-loader / loadSkills RPC 客户端', () => {
  it('把 skillsDir / conditions / exclude / ignoreConditions 映射到 RPC 参数', async () => {
    mocks.listSkills.mockResolvedValue([]);
    await loadSkills({
      skillsDir: '/custom/skills',
      conditions: ['tool:cflog_replay_card'],
      excludeSkillNames: ['plan-mode'],
      ignoreConditions: true,
    });
    expect(mocks.listSkills).toHaveBeenCalledWith({
      roots: ['/custom/skills'],
      conditions: ['tool:cflog_replay_card'],
      exclude: ['plan-mode'],
      ignoreConditions: true,
    });
  });

  it('缺省 roots 为内置目录 + 用户目录（用户覆盖内置）', async () => {
    mocks.listSkills.mockResolvedValue([]);
    await loadSkills({ ignoreConditions: true });
    const call = mocks.listSkills.mock.calls[0][0];
    expect(call.roots).toEqual([getSkillsDir(), path.join(mocks.userDataDir, 'skills')]);
  });

  it('工作区 extra roots 插在内置与用户目录之间，缺目录的跳过', async () => {
    const extra = fs.mkdtempSync(path.join(os.tmpdir(), 'workspace-skills-'));
    const missing = path.join(os.tmpdir(), 'skill-loader-missing-skills-does-not-exist');
    setWorkspaceSkillRootsProvider(() => [extra, missing]);
    mocks.listSkills.mockResolvedValue([]);
    await loadSkills({ ignoreConditions: true });
    const call = mocks.listSkills.mock.calls[0][0];
    expect(call.roots).toEqual([
      getSkillsDir(),
      path.resolve(extra),
      path.join(mocks.userDataDir, 'skills'),
    ]);
  });

  it('classifySkillOrigin 区分 builtin / user / workspace', () => {
    expect(classifySkillOrigin(getSkillsDir())).toBe('builtin');
    expect(classifySkillOrigin(path.join(mocks.userDataDir, 'skills'))).toBe('user');
    expect(classifySkillOrigin('/tmp/some-project/skills')).toBe('workspace');
  });

  it('listSkillRoots 在未注册 provider 时只有内置 + 用户', () => {
    expect(listSkillRoots()).toEqual([getSkillsDir(), path.join(mocks.userDataDir, 'skills')]);
  });

  it('返回 supervisor 解析好的模块', async () => {
    const skill = makeSkill({ name: 'cflog', dirName: '90-cflog' });
    mocks.listSkills.mockResolvedValue([skill]);
    const modules = await loadSkills({ skillsDir: '/x' });
    expect(modules).toEqual([skill]);
  });

  it('sidecar 不可用时返回空数组（提示词退回内置最小提示词）', async () => {
    mocks.sidecarEnabled = false;
    mocks.whenResolves = 'null';
    const modules = await loadSkills({ skillsDir: '/x' });
    expect(modules).toEqual([]);
    expect(mocks.listSkills).not.toHaveBeenCalled();
  });

  it('启动竞态：handle 未就绪但 boot 在飞时，等待就绪后正常加载', async () => {
    mocks.sidecarEnabled = false; // getSidecarSupervisor() → null（启动窗口期）
    mocks.whenResolves = 'supervisor';
    const skill = makeSkill({ name: 'cflog', dirName: '90-cflog' });
    mocks.listSkills.mockResolvedValue([skill]);
    const modules = await loadSkills({ skillsDir: '/x' });
    expect(modules).toEqual([skill]);
    expect(mocks.listSkills).toHaveBeenCalledOnce();
  });

  it('启动竞态：boot 失败/超时（when 解决为 null）仍退化为空数组', async () => {
    mocks.sidecarEnabled = false;
    mocks.whenResolves = 'null';
    const modules = await loadSkills({ skillsDir: '/x' });
    expect(modules).toEqual([]);
    expect(mocks.listSkills).not.toHaveBeenCalled();
  });

  it('RPC 失败时返回空数组而不是抛错', async () => {
    mocks.listSkills.mockRejectedValue(new Error('sidecar down'));
    const modules = await loadSkills({ skillsDir: '/x' });
    expect(modules).toEqual([]);
  });
});

describe('skill-loader / findSkill 别名解析', () => {
  const USER = makeSkill({
    name: 'my-workspace',
    displayName: '读工区技能',
    dirName: 'aa-user-skill',
    content: 'USER_SKILL_PROCEDURE',
  });

  beforeEach(() => {
    mocks.listSkills.mockResolvedValue([USER]);
  });

  it('按 name / dirName / displayName（中文名）解析，大小写不敏感', async () => {
    expect((await findSkill('my-workspace', { skillsDir: '/x' }))?.dirName).toBe('aa-user-skill');
    expect((await findSkill('AA-User-Skill', { skillsDir: '/x' }))?.name).toBe('my-workspace');
    expect((await findSkill('读工区技能', { skillsDir: '/x' }))?.name).toBe('my-workspace');
  });

  it('未命中返回 null', async () => {
    expect(await findSkill('nonexistent', { skillsDir: '/x' })).toBeNull();
  });

  it('空名字直接返回 null，不调 RPC', async () => {
    expect(await findSkill('  ', { skillsDir: '/x' })).toBeNull();
    expect(mocks.listSkills).not.toHaveBeenCalled();
  });

  it('显式触发绕过条件（ignoreConditions: true）并带上 exclude', async () => {
    await findSkill('my-workspace', { skillsDir: '/x', exclude: ['plan-mode'] });
    expect(mocks.listSkills).toHaveBeenCalledWith(
      expect.objectContaining({ ignoreConditions: true, exclude: ['plan-mode'] }),
    );
  });
});
