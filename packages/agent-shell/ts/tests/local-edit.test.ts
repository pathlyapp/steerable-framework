import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

// 编辑算法已下沉到框架（Python file_edit.py，单一真源），桌面 local-edit 只是
// `workspace.apply_edits` 的薄客户端。这里 mock 掉 supervisor 边界来测线协议
// 转换与错误映射；算法本身的语义由 packages/sidecar/py/tests/test_file_edit.py 覆盖。
const mocks = vi.hoisted(() => ({
  applyEdits: vi.fn(),
  sidecarEnabled: true,
}));
vi.mock('../src/sidecar/handle.js', () => ({
  getSidecarSupervisor: () => (mocks.sidecarEnabled ? { applyEdits: mocks.applyEdits } : null),
}));

import { applyEdits, buildApplyEditsResult, EditError } from '../src/local-edit.js';
import { hashContent, LocalExecutor } from '../src/local-executor.js';
import { SidecarMethodError } from '../src/sidecar/errors.js';

beforeEach(() => {
  mocks.applyEdits.mockReset();
  mocks.sidecarEnabled = true;
});

describe('applyEdits · sidecar RPC 薄客户端', () => {
  it('把 content/edits/filePath 发给 workspace.apply_edits 并映射结果', async () => {
    mocks.applyEdits.mockResolvedValue({
      content: 'hello world\nbaz qux\n',
      diff: '--- a/f\n+++ b/f\n',
      applied: 1,
      matches: [{ level: 'exact', startLine: 1, oldLineCount: 1 }],
    });
    const out = await applyEdits('hello world\nfoo bar\n', [{ oldText: 'foo bar', newText: 'baz qux' }], 'f');
    expect(mocks.applyEdits).toHaveBeenCalledWith({
      content: 'hello world\nfoo bar\n',
      edits: [{ oldText: 'foo bar', newText: 'baz qux' }],
      filePath: 'f',
    });
    expect(out.content).toBe('hello world\nbaz qux\n');
    expect(out.matches).toEqual([{ level: 'exact', startLine: 1, oldLineCount: 1 }]);
  });

  it('edit_failed 错误映射回 EditError 并保留 code', async () => {
    mocks.applyEdits.mockRejectedValue(
      new SidecarMethodError('锚点未找到', -32030, 'edit_failed', { code: 'not_found' }),
    );
    await expect(applyEdits('abc\n', [{ oldText: 'xyz', newText: 'q' }])).rejects.toThrowError(EditError);
    await applyEdits('abc\n', [{ oldText: 'xyz', newText: 'q' }]).catch((e) => {
      expect((e as EditError).code).toBe('not_found');
    });
  });

  it('非 edit_failed 的 sidecar 错误原样抛出（不吞传输/系统错误）', async () => {
    mocks.applyEdits.mockRejectedValue(new SidecarMethodError('boom', -32000, 'timeout', undefined));
    await expect(applyEdits('a\n', [{ oldText: 'a', newText: 'b' }])).rejects.toThrowError('boom');
  });

  it('sidecar 不可用 → 抛出明确错误（编辑算法在框架侧）', async () => {
    mocks.sidecarEnabled = false;
    await expect(applyEdits('a\n', [{ oldText: 'a', newText: 'b' }])).rejects.toThrowError(/sidecar/);
  });
});

describe('buildApplyEditsResult', () => {
  it('保留 content/diff/matches', () => {
    const out = buildApplyEditsResult({
      content: 'x\n',
      diff: 'd',
      matches: [{ level: 'trim', startLine: 3, oldLineCount: 2 }],
    });
    expect(out).toEqual({
      content: 'x\n',
      diff: 'd',
      matches: [{ level: 'trim', startLine: 3, oldLineCount: 2 }],
    });
  });
});

describe('LocalExecutor.editLocalFile · fs 编排（算法走注入 stub）', () => {
  async function withTempFile(
    initial: string,
    run: (executor: LocalExecutor, filePath: string, stub: ReturnType<typeof vi.fn>) => Promise<void>,
  ) {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'edit-test-'));
    const filePath = path.join(dir, 'target.txt');
    await writeFile(filePath, initial, 'utf-8');
    const stub = vi.fn();
    try {
      await run(new LocalExecutor(undefined, stub), filePath, stub);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }

  it('读文件→调算法→原子落盘，返回 version/diff/applied', async () => {
    await withTempFile('foo = 1\nbar = 2\n', async (executor, filePath, stub) => {
      stub.mockResolvedValue({
        content: 'foo = 1\nbar = 20\n',
        diff: '-bar = 2\n+bar = 20\n',
        matches: [{ level: 'exact', startLine: 1, oldLineCount: 1 }],
      });
      await executor.readLocalFile({ path: filePath }); // 硬门默认开：先读后改
      const res = await executor.editLocalFile({
        path: filePath,
        edits: [{ oldText: 'bar = 2', newText: 'bar = 20' }],
      });
      expect(stub).toHaveBeenCalledWith('foo = 1\nbar = 2\n', [{ oldText: 'bar = 2', newText: 'bar = 20' }], 'target.txt');
      expect(res.success).toBe(true);
      expect(res.applied).toBe(1);
      expect(res.version).toBeTruthy();
      expect(res.diff).toContain('+bar = 20');
      expect(await readFile(filePath, 'utf-8')).toBe('foo = 1\nbar = 20\n');
    });
  });

  it('read 返回的 version 可用于 edit 的 expectedVersion（一致 → 通过）', async () => {
    await withTempFile('x = 1\n', async (executor, filePath, stub) => {
      stub.mockResolvedValue({ content: 'x = 2\n', diff: '', matches: [{ level: 'exact', startLine: 0, oldLineCount: 1 }] });
      const read = await executor.readLocalFile({ path: filePath });
      const res = await executor.editLocalFile({
        path: filePath,
        edits: [{ oldText: 'x = 1', newText: 'x = 2' }],
        expectedVersion: read.version,
      });
      expect(res.success).toBe(true);
    });
  });

  it('expectedVersion 不匹配 → 冲突拒绝，不调算法、文件不被改动', async () => {
    await withTempFile('x = 1\n', async (executor, filePath, stub) => {
      const read = await executor.readLocalFile({ path: filePath });
      await writeFile(filePath, 'x = 999\n', 'utf-8'); // 外部改动
      const res = await executor.editLocalFile({
        path: filePath,
        edits: [{ oldText: 'x = 1', newText: 'x = 2' }],
        expectedVersion: read.version,
      });
      expect(res.success).toBe(false);
      expect(res.error).toContain('冲突');
      expect(stub).not.toHaveBeenCalled();
      expect(await readFile(filePath, 'utf-8')).toBe('x = 999\n');
    });
  });

  it('算法抛 EditError → 不写盘、返回失败', async () => {
    await withTempFile('alpha\nbeta\n', async (executor, filePath, stub) => {
      stub.mockRejectedValue(new EditError('锚点未找到', 'not_found'));
      await executor.readLocalFile({ path: filePath }); // 硬门默认开：先读后改
      const res = await executor.editLocalFile({
        path: filePath,
        edits: [{ oldText: 'gamma', newText: 'x' }],
      });
      expect(res.success).toBe(false);
      expect(res.error).toContain('锚点未找到');
      expect(await readFile(filePath, 'utf-8')).toBe('alpha\nbeta\n');
    });
  });

  it('writeLocalFile 的 expectedVersion 冲突也被拒绝', async () => {
    await withTempFile('v1\n', async (executor, filePath) => {
      const read = await executor.readLocalFile({ path: filePath });
      await writeFile(filePath, 'v2\n', 'utf-8');
      const res = await executor.writeLocalFile({
        path: filePath,
        content: 'v3\n',
        expectedVersion: read.version,
      });
      expect(res.success).toBe(false);
      expect(res.error).toContain('冲突');
      expect(await readFile(filePath, 'utf-8')).toBe('v2\n');
    });
  });

  it('项目根围栏：越界路径被拒绝（不调算法）', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'edit-root-'));
    const outside = await mkdtemp(path.join(os.tmpdir(), 'edit-outside-'));
    try {
      const stub = vi.fn();
      const executor = new LocalExecutor(undefined, stub);
      const target = path.join(outside, 'x.txt');
      await writeFile(target, 'a\n', 'utf-8');
      const res = await executor.editLocalFile(
        { path: target, edits: [{ oldText: 'a', newText: 'b' }] },
        dir, // projectRoot = dir，target 在其外
      );
      expect(res.success).toBe(false);
      expect(res.error).toContain('越界');
      expect(stub).not.toHaveBeenCalled();
    } finally {
      await rm(dir, { recursive: true, force: true });
      await rm(outside, { recursive: true, force: true });
    }
  });
});

describe('LocalExecutor readFileState · P2b 自动 CAS / 硬门 / seed', () => {
  async function withTempDir(run: (dir: string) => Promise<void>) {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'rbw-test-'));
    try {
      await run(dir);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }

  function withRequireReadBeforeWrite<T>(run: () => Promise<T>): Promise<T> {
    const prev = process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE;
    process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE = '1';
    return run().finally(() => {
      if (prev === undefined) delete process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE;
      else process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE = prev;
    });
  }

  it('read 后 write 无需 expectedVersion：自动 CAS 通过并回写新版本（连续写不互斥）', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'v1\n', 'utf-8');
      const executor = new LocalExecutor();
      await executor.readLocalFile({ path: filePath });

      const first = await executor.writeLocalFile({ path: filePath, content: 'v2\n' });
      expect(first.success).toBe(true);
      // 写成功回写读证据：第二次写（基于本会话自己的写入）也直接通过。
      const second = await executor.writeLocalFile({ path: filePath, content: 'v3\n' });
      expect(second.success).toBe(true);
      expect(await readFile(filePath, 'utf-8')).toBe('v3\n');
    });
  });

  it('read 后外部改动 → 自动 CAS 拒绝（不带 expectedVersion），文件不被覆盖', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'v1\n', 'utf-8');
      const executor = new LocalExecutor();
      await executor.readLocalFile({ path: filePath });
      await writeFile(filePath, 'external\n', 'utf-8'); // 会话外改动

      const res = await executor.writeLocalFile({ path: filePath, content: 'mine\n' });
      expect(res.success).toBe(false);
      expect(res.error).toContain('冲突');
      expect(await readFile(filePath, 'utf-8')).toBe('external\n');
    });
  });

  it('edit 的自动 CAS：read 后外部改动 → 拒绝且不调算法', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'x = 1\n', 'utf-8');
      const stub = vi.fn();
      const executor = new LocalExecutor(undefined, stub);
      await executor.readLocalFile({ path: filePath });
      await writeFile(filePath, 'x = 999\n', 'utf-8');

      const res = await executor.editLocalFile({
        path: filePath,
        edits: [{ oldText: 'x = 1', newText: 'x = 2' }],
      });
      expect(res.success).toBe(false);
      expect(res.error).toContain('冲突');
      expect(stub).not.toHaveBeenCalled();
    });
  });

  it('读后文件被删除再写入按新建放行（隐式期望 missingOk）', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'v1\n', 'utf-8');
      const executor = new LocalExecutor();
      await executor.readLocalFile({ path: filePath });
      await rm(filePath);

      const res = await executor.writeLocalFile({ path: filePath, content: 'recreated\n' });
      expect(res.success).toBe(true);
      expect(await readFile(filePath, 'utf-8')).toBe('recreated\n');
    });
  });

  it('未读已存在文件：默认硬门开启拒绝；env=0 显式关闭后允许', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'v1\n', 'utf-8');
      const executor = new LocalExecutor();
      const blocked = await executor.writeLocalFile({ path: filePath, content: 'v2\n' });
      expect(blocked.success).toBe(false);
      expect(blocked.error).toContain('未读先写已拒绝');
    });
    // 显式退出（STEERABLE_REQUIRE_READ_BEFORE_WRITE=0）恢复旧行为。
    const prev = process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE;
    process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE = '0';
    try {
      await withTempDir(async (dir) => {
        const filePath = path.join(dir, 'a.txt');
        await writeFile(filePath, 'v1\n', 'utf-8');
        const executor = new LocalExecutor();
        const res = await executor.writeLocalFile({ path: filePath, content: 'v2\n' });
        expect(res.success).toBe(true);
      });
    } finally {
      if (prev === undefined) delete process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE;
      else process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE = prev;
    }
  });

  it('硬门：未读已存在文件拒绝写与改；读后放行；新建不拦截', async () => {
    await withRequireReadBeforeWrite(async () => {
      await withTempDir(async (dir) => {
        const existing = path.join(dir, 'existing.txt');
        await writeFile(existing, 'v1\n', 'utf-8');
        const stub = vi.fn().mockResolvedValue({
          content: 'v2\n',
          diff: '',
          matches: [{ level: 'exact', startLine: 0, oldLineCount: 1 }],
        });
        const executor = new LocalExecutor(undefined, stub);

        const writeRes = await executor.writeLocalFile({ path: existing, content: 'v2\n' });
        expect(writeRes.success).toBe(false);
        expect(writeRes.error).toContain('未读先写');

        const editRes = await executor.editLocalFile({
          path: existing,
          edits: [{ oldText: 'v1', newText: 'v2' }],
        });
        expect(editRes.success).toBe(false);
        expect(editRes.error).toContain('未读先改');
        expect(stub).not.toHaveBeenCalled();

        // 新建文件不受硬门限制。
        const fresh = path.join(dir, 'fresh.txt');
        const createRes = await executor.writeLocalFile({ path: fresh, content: 'new\n' });
        expect(createRes.success).toBe(true);

        // 读后放行。
        await executor.readLocalFile({ path: existing });
        const afterRead = await executor.writeLocalFile({ path: existing, content: 'v2\n' });
        expect(afterRead.success).toBe(true);
      });
    });
  });

  it('seedReadState 重灌的证据参与自动 CAS（resume 后无需重读）', async () => {
    await withTempDir(async (dir) => {
      const filePath = path.join(dir, 'a.txt');
      await writeFile(filePath, 'v1\n', 'utf-8');
      // 模拟进程重启：全新 executor，readFileState 为空，由 sidecar 的
      // read_state.seed 推入上一会话的读证据。
      const executor = new LocalExecutor();
      const seeded = executor.seedReadState({
        [filePath]: hashContent('v1\n'),
        'bad-entry': 42 as unknown as string,
      });
      expect(seeded).toBe(1);

      const ok = await executor.writeLocalFile({ path: filePath, content: 'v2\n' });
      expect(ok.success).toBe(true);

      // 种子版本与当前内容不符 → 冲突（记录里的版本已过时）。
      const stale = new LocalExecutor();
      stale.seedReadState({ [filePath]: hashContent('v0\n') });
      const conflict = await stale.writeLocalFile({ path: filePath, content: 'v3\n' });
      expect(conflict.success).toBe(false);
      expect(conflict.error).toContain('冲突');
    });
  });
});
