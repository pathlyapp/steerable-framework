/**
 * 部分视图门（框架 workspace_tools partial_reads / CC isPartialView 对齐）。
 *
 * local_read_file 支持 offset/limit 行分页与超长裁剪，部分视图结果带
 * `partial: true` 与省略标记；本会话只读到部分内容的路径拒绝整体覆写
 * （writeLocalFile），定点编辑（editLocalFile）不受此限。version 永远基于
 * 完整内容，部分视图的 CAS 令牌保持有效。
 */
import { describe, expect, it } from 'vitest';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { LocalExecutor, slicePartialView, hashContent } from '../src/local-executor.js';
import type { ApplyEditsResult } from '../src/local-edit.js';

const stubApplyEdits = async (content: string): Promise<ApplyEditsResult> => ({
  content: `${content}\nedited`,
  diff: '--- a/f\n+++ b/f\n',
  matches: [{ level: 'exact', startLine: 0, oldLineCount: 1 }],
});

async function setup(lines: number | string): Promise<{ executor: LocalExecutor; dir: string; file: string }> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'partial-view-'));
  const file = path.join(dir, 'f.txt');
  const body = typeof lines === 'string' ? lines : Array.from({ length: lines }, (_, i) => `line-${i + 1}`).join('\n');
  await writeFile(file, body, 'utf-8');
  return { executor: new LocalExecutor(undefined, stubApplyEdits), dir, file };
}

describe('slicePartialView · 分页与裁剪', () => {
  it('无 offset/limit 且未超长：原样返回，partial=false', () => {
    expect(slicePartialView('a\nb\nc')).toEqual({ content: 'a\nb\nc', partial: false });
  });

  it('offset/limit 切片：中段带两侧省略标记与继续 offset', () => {
    const body = Array.from({ length: 10 }, (_, i) => `L${i + 1}`).join('\n');
    const view = slicePartialView(body, 4, 3);
    expect(view.partial).toBe(true);
    expect(view.content).toBe('...[{上文省略 3 行}]...\nL4\nL5\nL6\n...[{下文省略 4 行；用 offset=7 继续}]...');
  });

  it('从第 1 行开始取 limit 行：只有下文省略标记', () => {
    const view = slicePartialView('a\nb\nc\nd', 1, 2);
    expect(view).toEqual({ content: 'a\nb\n...[{下文省略 2 行；用 offset=3 继续}]...', partial: true });
  });

  it('offset 到文件尾：只有上文省略标记', () => {
    const view = slicePartialView('a\nb\nc', 3, 5);
    expect(view).toEqual({ content: '...[{上文省略 2 行}]...\nc', partial: true });
  });

  it('分页参数覆盖全文：partial=false（视图即全文）', () => {
    const view = slicePartialView('a\nb', 1, 2);
    expect(view).toEqual({ content: 'a\nb', partial: false });
  });

  it('offset/limit 非法：0、负数、非整数、越界都报错', () => {
    expect(() => slicePartialView('a\nb', 0)).toThrow(/offset/);
    expect(() => slicePartialView('a\nb', 1.5)).toThrow(/offset/);
    expect(() => slicePartialView('a\nb', 3)).toThrow(/超出文件行数/);
    expect(() => slicePartialView('a\nb', 1, 0)).toThrow(/limit/);
  });

  it('超长未分页：头尾裁剪，保留尾部并打省略字符数标记', () => {
    const body = 'h'.repeat(40_000) + 'm'.repeat(60_000) + 't'.repeat(40_000);
    const view = slicePartialView(body);
    expect(view.partial).toBe(true);
    expect(view.content).toContain('省略 40000 字符');
    expect(view.content.startsWith('h'.repeat(20_000))).toBe(true);
    expect(view.content.endsWith('t'.repeat(40_000))).toBe(true);
    expect(view.content.length).toBeLessThan(body.length);
  });
});

describe('部分视图门 · read/write/edit 联动', () => {
  it('完整读取后整体覆写允许（partial=false 不登记）', async () => {
    const { executor, dir, file } = await setup(5);
    try {
      const read = await executor.readLocalFile({ path: file });
      expect(read.success).toBe(true);
      expect(read.partial).toBe(false);
      const write = await executor.writeLocalFile({ path: file, content: 'new' });
      expect(write.success).toBe(true);
      expect(await readFile(file, 'utf-8')).toBe('new');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('分页读取 → 整体覆写被拒绝且不落盘；错误信息指向 edit/分段读', async () => {
    const { executor, dir, file } = await setup(10);
    try {
      const read = await executor.readLocalFile({ path: file, offset: 1, limit: 3 });
      expect(read.success).toBe(true);
      expect(read.partial).toBe(true);
      expect(read.content).toContain('line-1');
      expect(read.content).not.toContain('line-4');

      const write = await executor.writeLocalFile({ path: file, content: 'blind overwrite' });
      expect(write.success).toBe(false);
      expect(write.error).toContain('拒绝整体覆写');
      expect(write.error).toContain('local_edit_file');
      // 拒绝时不写盘
      expect(await readFile(file, 'utf-8')).toContain('line-10');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('分页读取后定点编辑允许（编辑不销记门，后续盲写仍拒绝）', async () => {
    const { executor, dir, file } = await setup(10);
    try {
      await executor.readLocalFile({ path: file, offset: 1, limit: 2 });
      const edit = await executor.editLocalFile({ path: file, edits: [{ oldText: 'line-1', newText: 'L1' }] });
      expect(edit.success).toBe(true);
      const write = await executor.writeLocalFile({ path: file, content: 'still blind' });
      expect(write.success).toBe(false);
      expect(write.error).toContain('拒绝整体覆写');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('分页读取后补一次完整读取：门销记，整体覆写放行', async () => {
    const { executor, dir, file } = await setup(10);
    try {
      await executor.readLocalFile({ path: file, offset: 5, limit: 2 });
      const full = await executor.readLocalFile({ path: file });
      expect(full.partial).toBe(false);
      const write = await executor.writeLocalFile({ path: file, content: 'full rewrite' });
      expect(write.success).toBe(true);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('分页读取的 version 与完整读取一致（基于完整内容，CAS 保持有效）', async () => {
    const { executor, dir, file } = await setup(10);
    try {
      const paged = await executor.readLocalFile({ path: file, offset: 2, limit: 2 });
      const full = await executor.readLocalFile({ path: file });
      expect(paged.version).toBe(full.version);
      expect(paged.version).toBe(hashContent(await readFile(file, 'utf-8')));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('本会话自己的整体覆写成功后销记（先全文读再写，后续写不受门影响）', async () => {
    const { executor, dir, file } = await setup(3);
    try {
      await executor.readLocalFile({ path: file });
      const w1 = await executor.writeLocalFile({ path: file, content: 'a\nb\nc\nd\ne' });
      expect(w1.success).toBe(true);
      // 写成功后 readFileState 已回写新版本；再分页读 → 登记；再全文读 → 销记。
      await executor.readLocalFile({ path: file, offset: 1, limit: 1 });
      const blocked = await executor.writeLocalFile({ path: file, content: 'x' });
      expect(blocked.success).toBe(false);
      await executor.readLocalFile({ path: file });
      const w2 = await executor.writeLocalFile({ path: file, content: 'x' });
      expect(w2.success).toBe(true);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('非法 offset 返回错误结果而不是抛异常', async () => {
    const { executor, dir, file } = await setup(3);
    try {
      const read = await executor.readLocalFile({ path: file, offset: 99 });
      expect(read.success).toBe(false);
      expect(read.error).toContain('超出文件行数');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
