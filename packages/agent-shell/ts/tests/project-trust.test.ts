import { describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { ProjectRegistry, type ProjectRecord } from '../src/project-registry.js';
import { loadProjectRuleFiles } from '../src/project-rules.js';

function makeMemoryStore() {
  let data: ProjectRecord[] = [];
  return {
    get: (key: 'projects') => (key === 'projects' ? data : undefined),
    set: (key: 'projects', value: ProjectRecord[]) => {
      if (key === 'projects') data = value;
    },
  };
}

describe('project-registry / 信任门控（W6-5）', () => {
  it('新建项目默认不信任（fail-closed）', () => {
    const registry = new ProjectRegistry(makeMemoryStore());
    const p = registry.create({ name: 'demo', folderPath: '/tmp/demo' });
    expect(p.trusted).toBeFalsy();
    expect(registry.isTrusted(p.id)).toBe(false);
  });

  it('旧记录没有 trusted 字段时按不信任处理', () => {
    const registry = new ProjectRegistry(makeMemoryStore());
    const p = registry.create({ name: 'demo', folderPath: '/tmp/demo' });
    // 模拟旧记录：字段缺失
    expect(registry.isTrusted(p.id)).toBe(false);
  });

  it('setTrusted 授予/撤销信任并持久化', () => {
    const store = makeMemoryStore();
    const registry = new ProjectRegistry(store);
    const p = registry.create({ name: 'demo', folderPath: '/tmp/demo' });

    const granted = registry.setTrusted(p.id, true);
    expect(granted.trusted).toBe(true);
    expect(registry.isTrusted(p.id)).toBe(true);

    // 重新构造 registry（模拟重启）后信任状态仍在
    const reopened = new ProjectRegistry(store);
    expect(reopened.isTrusted(p.id)).toBe(true);

    const revoked = reopened.setTrusted(p.id, false);
    expect(revoked.trusted).toBe(false);
    expect(reopened.isTrusted(p.id)).toBe(false);
  });

  it('setTrusted 对不存在的项目抛错', () => {
    const registry = new ProjectRegistry(makeMemoryStore());
    expect(() => registry.setTrusted('nope', true)).toThrow(/不存在/);
  });
});

describe('project-rules / 规则文件加载（W6-7a）', () => {
  it('发现项目目录里的 AGENTS.md 并注入内容', () => {
    const dir = mkdtempSync(join(tmpdir(), 'proj-rules-'));
    try {
      writeFileSync(join(dir, 'AGENTS.md'), '# 项目约定\n一律用 pnpm。');
      const rules = loadProjectRuleFiles(dir);
      expect(rules.files).toHaveLength(1);
      expect(rules.content).toContain('一律用 pnpm');
      expect(rules.content).toContain('AGENTS.md');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('向上遍历收集父目录规则，叶目录（更具体）排在后面', () => {
    const root = mkdtempSync(join(tmpdir(), 'proj-rules-'));
    try {
      const sub = join(root, 'packages', 'app');
      mkdirSync(sub, { recursive: true });
      writeFileSync(join(root, 'AGENTS.md'), 'ROOT 规则');
      writeFileSync(join(sub, 'CLAUDE.md'), 'LEAF 规则');
      const rules = loadProjectRuleFiles(sub);
      expect(rules.files).toHaveLength(2);
      const rootIdx = rules.content.indexOf('ROOT 规则');
      const leafIdx = rules.content.indexOf('LEAF 规则');
      expect(rootIdx).toBeGreaterThanOrEqual(0);
      expect(leafIdx).toBeGreaterThan(rootIdx);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('同层 override 优先：AGENTS.override.md 与 AGENTS.md 都被收集', () => {
    const dir = mkdtempSync(join(tmpdir(), 'proj-rules-'));
    try {
      writeFileSync(join(dir, 'AGENTS.md'), 'BASE');
      writeFileSync(join(dir, 'AGENTS.override.md'), 'OVERRIDE');
      const rules = loadProjectRuleFiles(dir);
      expect(rules.files).toHaveLength(2);
      expect(rules.content.indexOf('OVERRIDE')).toBeLessThan(rules.content.indexOf('BASE'));
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('没有规则文件时返回空', () => {
    const dir = mkdtempSync(join(tmpdir(), 'proj-rules-'));
    try {
      const rules = loadProjectRuleFiles(dir);
      expect(rules.files).toEqual([]);
      expect(rules.content).toBe('');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('单个超长文件被截断并标记 truncated', () => {
    const dir = mkdtempSync(join(tmpdir(), 'proj-rules-'));
    try {
      writeFileSync(join(dir, 'AGENTS.md'), 'x'.repeat(20_000));
      const rules = loadProjectRuleFiles(dir);
      expect(rules.truncated).toBe(true);
      expect(rules.content.length).toBeLessThan(20_000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('空路径 / 不存在的目录返回空', () => {
    expect(loadProjectRuleFiles('')).toEqual({ files: [], content: '', truncated: false });
    expect(loadProjectRuleFiles('/definitely/not/a/real/dir-xyz')).toEqual({
      files: [],
      content: '',
      truncated: false,
    });
  });
});
