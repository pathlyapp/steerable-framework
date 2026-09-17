import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { resolveMentionedPaths } from '../../src/local-backend/mentioned-paths.js';

/**
 * resolveMentionedPaths：正文里提到的路径能否落地成存在的绝对路径。
 * 形状判断在渲染层，这里只管「落地 + stat 证伪」。
 */

let base: string;
let home: string;

beforeAll(async () => {
  base = await fs.mkdtemp(path.join(os.tmpdir(), 'mentioned-base-'));
  home = await fs.mkdtemp(path.join(os.tmpdir(), 'mentioned-home-'));
  await fs.writeFile(path.join(base, '自我介绍.pptx'), 'ppt');
  await fs.mkdir(path.join(base, 'docs'));
  await fs.writeFile(path.join(base, 'docs', 'report.md'), '# r');
  await fs.writeFile(path.join(home, '季度报告.pdf'), 'pdf');
});

afterAll(async () => {
  await fs.rm(base, { recursive: true, force: true });
  await fs.rm(home, { recursive: true, force: true });
});

describe('resolveMentionedPaths', () => {
  it('相对路径按 baseDir 落地，./ 前缀与裸文件名都认', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['./自我介绍.pptx', 'docs/report.md'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([
      { candidate: './自我介绍.pptx', path: path.join(base, '自我介绍.pptx'), isDirectory: false },
      { candidate: 'docs/report.md', path: path.join(base, 'docs', 'report.md'), isDirectory: false },
    ]);
  });

  it('绝对路径原样落地，与 baseDir 无关', async () => {
    const target = path.join(home, '季度报告.pdf');
    const resolved = await resolveMentionedPaths({
      candidates: [target],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([{ candidate: target, path: target, isDirectory: false }]);
  });

  it('~/ 展开到 homeDir', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['~/季度报告.pdf'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([
      { candidate: '~/季度报告.pdf', path: path.join(home, '季度报告.pdf'), isDirectory: false },
    ]);
  });

  it('目录带 isDirectory 标记（渲染层据此换图标/文案）', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['docs'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([
      { candidate: 'docs', path: path.join(base, 'docs'), isDirectory: true },
    ]);
  });

  it('不存在的候选不回（模型写错的路径不给可点击的假象）', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['./不存在.pptx', 'docs/nope.md', '/tmp/definitely-missing-xyz.txt'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([]);
  });

  it('重复候选只回一条', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['./自我介绍.pptx', './自我介绍.pptx'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toHaveLength(1);
  });

  it('含换行的候选被丢弃（不是单个路径字面量）', async () => {
    const resolved = await resolveMentionedPaths({
      candidates: ['./自我介绍.pptx\nrm -rf /'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([]);
  });

  it('候选数量超过 64 时只处理前 64 个', async () => {
    const filler = Array.from({ length: 64 }, (_, i) => `./missing-${i}.txt`);
    const resolved = await resolveMentionedPaths({
      candidates: [...filler, './自我介绍.pptx'],
      baseDir: base,
      homeDir: home,
    });
    expect(resolved).toEqual([]);
  });
});
