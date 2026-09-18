/**
 * slash-sources：`/` 令牌背后的技能 + MCP 工具目录。
 * 锁定：隐藏技能的三种命中方式（内建名 / 目录 id / 包注册表）、
 * SLASH_TOKEN_PATTERN 的真实匹配边界（行首或空白后、不含内层斜杠——
 * 路径排除发生在 resolve 阶段而不是正则阶段）、resolveSlashTool 的
 * 优先级（MCP 精确 token > mcp__ 前缀直通 > 技能名 / displayName，
 * 隐藏技能永不解析）、refreshSlashSources 的降级（MCP 失败不影响技能）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { registerPackRenderer } from '../packs/registry';
import {
  isHiddenSlashSkill,
  refreshSlashSources,
  resolveSlashTool,
  SLASH_TOKEN_PATTERN,
  type McpToolItem,
  type SkillItem,
} from './slash-sources';

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
  vi.restoreAllMocks();
});

describe('isHiddenSlashSkill', () => {
  it('内建技能按 name 或目录 id 命中', () => {
    expect(isHiddenSlashSkill({ name: 'plan-mode', description: '' })).toBe(true);
    expect(isHiddenSlashSkill({ name: 'identity', description: '' })).toBe(true);
    expect(
      isHiddenSlashSkill({ name: '计划', id: '70-plan-mode', description: '' }),
    ).toBe(true);
  });

  it('业务技能不隐藏', () => {
    expect(isHiddenSlashSkill({ name: 'commit', description: '' })).toBe(false);
  });

  it('包渲染层声明的隐藏技能也命中', () => {
    registerPackRenderer('test-pack-slash', { hiddenSlashSkills: ['pack-secret'] });
    expect(isHiddenSlashSkill({ name: 'pack-secret', description: '' })).toBe(true);
  });
});

describe('SLASH_TOKEN_PATTERN 匹配边界', () => {
  /** 消费方用的是捕获组 1（完整匹配带前导空白），这里同样取捕获组。 */
  const tokens = (s: string) => [...s.matchAll(SLASH_TOKEN_PATTERN)].map((m) => m[1]);

  it('匹配行首或空白后的 /token', () => {
    expect(tokens('hello /foo world')).toEqual(['/foo']);
    expect(tokens('/foo /bar')).toEqual(['/foo', '/bar']);
  });

  it('紧跟在非空白后的斜杠不匹配（分数、相对路径、URL）', () => {
    expect(tokens('a/b 1/2')).toEqual([]);
    expect(tokens('https://a.com/b')).toEqual([]);
  });

  it('绝对路径里的内层斜杠会截断 token——路径排除靠 resolve 阶段', () => {
    // 正则只保证「空白后 + 无内层斜杠」；/mnt/c 被截成 /mnt，
    // 再由 resolveSlashTool 查目录落空来排除。
    expect(tokens('see /mnt/c')).toEqual(['/mnt']);
  });
});

describe('resolveSlashTool', () => {
  const skills: SkillItem[] = [
    { name: 'commit', description: '提交' },
    { name: 'x', displayName: '提交', description: '' },
    { name: 'plan-mode', description: '' },
  ];
  const mcpTools: McpToolItem[] = [
    { token: 'mcp__fs__read', toolName: 'read', serverKey: 'fs', serverName: 'FS', description: '' },
  ];

  it('MCP 精确 token 命中（大小写不敏感），label 保留用户输入', () => {
    expect(resolveSlashTool('mcp__fs__read', skills, mcpTools)).toEqual({
      type: 'mcp',
      id: 'mcp__fs__read',
      label: 'mcp__fs__read',
    });
    expect(resolveSlashTool('MCP__FS__READ', skills, mcpTools)?.id).toBe('mcp__fs__read');
  });

  it('mcp__ 前缀即使目录里没有也直通（服务器可能暂时断开）', () => {
    expect(resolveSlashTool('mcp__down__tool', [], [])).toEqual({
      type: 'mcp',
      id: 'mcp__down__tool',
      label: 'mcp__down__tool',
    });
  });

  it('技能按 name 或 displayName 命中，大小写不敏感', () => {
    expect(resolveSlashTool('Commit', skills, mcpTools)).toEqual({
      type: 'skill',
      id: 'commit',
      label: 'Commit',
    });
    expect(resolveSlashTool('提交', skills, mcpTools)).toEqual({
      type: 'skill',
      id: 'x',
      label: '提交',
    });
  });

  it('隐藏技能即使在目录里也不解析', () => {
    expect(resolveSlashTool('plan-mode', skills, mcpTools)).toBeNull();
  });

  it('什么都不匹配返回 null', () => {
    expect(resolveSlashTool('nosuch', skills, mcpTools)).toBeNull();
  });
});

describe('refreshSlashSources', () => {
  it('非 Electron 环境直接返回当前缓存，不发请求', async () => {
    const sources = await refreshSlashSources();
    expect(sources).toEqual({ skills: [], mcpTools: [] });
  });

  it('拉取两个端点并过滤隐藏技能', async () => {
    const request = vi.fn((input: { path: string }) => {
      if (input.path === '/api/v2/chat-agents/skills') {
        return Promise.resolve({
          skills: [
            { name: 'commit', description: '提交' },
            { name: 'plan-mode', description: '' },
          ],
        });
      }
      return Promise.resolve({ mcpTools: [{ token: 'mcp__fs__read' }] });
    });
    (window as { electron?: unknown }).electron = { localBackend: { request } };
    const sources = await refreshSlashSources();
    expect(sources.skills).toEqual([{ name: 'commit', description: '提交' }]);
    expect(sources.mcpTools).toEqual([{ token: 'mcp__fs__read' }]);
    expect(request).toHaveBeenCalledWith({
      method: 'GET',
      path: '/api/v2/chat-agents/skills',
    });
    expect(request).toHaveBeenCalledWith({
      method: 'GET',
      path: '/api/v2/chat-agents/mcp-tools',
    });
  });

  it('MCP 端点失败时保留旧缓存、技能结果照常更新', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const request = vi.fn((input: { path: string }) => {
      if (input.path === '/api/v2/chat-agents/skills') {
        return Promise.resolve({ skills: [{ name: 'lint', description: '' }] });
      }
      return Promise.reject(new Error('mcp down'));
    });
    (window as { electron?: unknown }).electron = { localBackend: { request } };
    const sources = await refreshSlashSources();
    expect(sources.skills).toEqual([{ name: 'lint', description: '' }]);
    // 模块级缓存的语义是「保留最近一次成功」：上一用例拉到的 MCP 工具还在。
    expect(sources.mcpTools).toEqual([{ token: 'mcp__fs__read' }]);
    expect(errorSpy).toHaveBeenCalled();
  });
});
