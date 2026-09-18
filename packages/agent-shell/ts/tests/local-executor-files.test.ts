/**
 * LocalExecutor 文件操作面：read/write 的基础分支与路径围栏、同文件
 * 串行化队列、原子写残留，以及 openLocalTarget 的 URL/路径分流。
 *
 * 与现有测试的分工：
 * - 部分视图门（offset/limit 分页、整体覆写拒绝）→ local-partial-view.test.ts
 * - read-before-write 硬门 / 自动 CAS / seedReadState → local-edit.test.ts
 * - editLocalFile 算法编排与契约形状 → local-edit.test.ts / tool-contract.test.ts
 *
 * 本文件覆盖：readLocalFile 的不存在 / 目录 / maxSize / encoding / `~` 展开，
 * writeLocalFile 的 createDirs / version 回执 / 覆盖写，isPathWithinRoot /
 * buildProjectRootViolation / additionalReadRoots 围栏，serializeFileOp 的
 * 并发语义，atomicWrite 的临时文件清理，openLocalTarget 的 he 解码 /
 * Apple Maps URL 规范化 / 系统打开错误映射（mock runtime 边界，不真打开）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtemp, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

// openLocalTarget 会调系统 `open`/浏览器——mock 掉 runtime 的系统打开边界，
// 只验证分流与参数转换，不在测试机上真弹窗。
const mocks = vi.hoisted(() => ({
  shellOpenExternal: vi.fn(),
  shellOpenPath: vi.fn(),
}));
vi.mock('../src/runtime.js', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/runtime.js')>();
  return {
    ...original,
    shellOpenExternal: mocks.shellOpenExternal,
    shellOpenPath: mocks.shellOpenPath,
  };
});

import {
  LocalExecutor,
  buildProjectRootViolation,
  hashContent,
  isPathWithinRoot,
} from '../src/local-executor.js';

async function withTempDir(run: (dir: string) => Promise<void>): Promise<void> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'exec-files-'));
  try {
    await run(dir);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

describe('readLocalFile · 基础分支', () => {
  it('文件不存在：success=false 且带错误消息', async () => {
    const res = await new LocalExecutor().readLocalFile({
      path: path.join(os.tmpdir(), `agent-shell-no-such-${process.pid}`),
    });
    expect(res.success).toBe(false);
    expect(res.error).toBeTruthy();
  });

  it('目标是目录：读取失败返回错误而不是抛异常', async () => {
    await withTempDir(async (dir) => {
      const res = await new LocalExecutor().readLocalFile({ path: dir });
      expect(res.success).toBe(false);
      expect(res.error).toBeTruthy();
    });
  });

  it('maxSize：超限拒绝并报告字节数；足够则成功', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'big.txt');
      await writeFile(file, 'x'.repeat(2048), 'utf-8');
      const executor = new LocalExecutor();

      const tooBig = await executor.readLocalFile({ path: file, maxSize: 1024 });
      expect(tooBig.success).toBe(false);
      expect(tooBig.error).toContain('File too large');
      expect(tooBig.error).toContain('2048');
      expect(tooBig.error).toContain('1024');

      const ok = await executor.readLocalFile({ path: file, maxSize: 2048 });
      expect(ok.success).toBe(true);
      expect(ok.content).toHaveLength(2048);
    });
  });

  it('version 是完整内容的 SHA-256（hex）', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'v.txt');
      await writeFile(file, 'hello\n', 'utf-8');
      const res = await new LocalExecutor().readLocalFile({ path: file });
      expect(res.success).toBe(true);
      expect(res.version).toBe(hashContent('hello\n'));
    });
  });

  it('encoding 参数生效：latin1 按单字节解码', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'enc.txt');
      // 0xE9 在 latin1 是 é；单独一个字节在 utf-8 非法 → 替换字符。
      await writeFile(file, Buffer.from([0x68, 0xe9]));
      const executor = new LocalExecutor();
      const latin = await executor.readLocalFile({ path: file, encoding: 'latin1' });
      expect(latin.success).toBe(true);
      expect(latin.content).toBe('hé');
      const utf = await executor.readLocalFile({ path: file, encoding: 'utf-8' });
      expect(utf.success).toBe(true);
      expect(utf.content).not.toBe('hé');
    });
  });

  it('`~` 展开到家目录', async () => {
    const name = `agent-shell-tilde-${process.pid}.txt`;
    const abs = path.join(os.homedir(), name);
    await writeFile(abs, 'tilde-ok', 'utf-8');
    try {
      const res = await new LocalExecutor().readLocalFile({ path: `~/${name}` });
      expect(res.success).toBe(true);
      expect(res.content).toBe('tilde-ok');
    } finally {
      await rm(abs, { force: true });
    }
  });
});

describe('writeLocalFile · 目录与版本', () => {
  it('createDirs=false：父目录不存在时失败；createDirs=true 自动建目录', async () => {
    await withTempDir(async (dir) => {
      const nested = path.join(dir, 'a', 'b', 'c.txt');
      const executor = new LocalExecutor();

      const fail = await executor.writeLocalFile({ path: nested, content: 'x' });
      expect(fail.success).toBe(false);
      expect(fail.error).toBeTruthy();

      const ok = await executor.writeLocalFile({ path: nested, content: 'x', createDirs: true });
      expect(ok.success).toBe(true);
      expect(await readFile(nested, 'utf-8')).toBe('x');
    });
  });

  it('返回的 version 与写入内容的 hashContent 一致', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'v.txt');
      const res = await new LocalExecutor().writeLocalFile({ path: file, content: 'v1\n' });
      expect(res.success).toBe(true);
      expect(res.version).toBe(hashContent('v1\n'));
    });
  });

  it('覆盖已存在文件：先读后写，旧内容被原子替换', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'f.txt');
      await writeFile(file, 'old-content', 'utf-8');
      const executor = new LocalExecutor();
      // 硬门默认开：已存在文件须先读（硬门/CAS 语义本身在 local-edit.test.ts）。
      await executor.readLocalFile({ path: file });
      const res = await executor.writeLocalFile({ path: file, content: 'new-content' });
      expect(res.success).toBe(true);
      expect(await readFile(file, 'utf-8')).toBe('new-content');
    });
  });
});

describe('路径围栏 · isPathWithinRoot / buildProjectRootViolation', () => {
  // 纯字符串判断（不碰 fs），用 path.resolve 构造平台相关的合法绝对路径。
  const root = path.resolve(path.join(os.tmpdir(), 'agent-root'));

  it('root 本身与子路径在界内', () => {
    expect(isPathWithinRoot(root, root)).toBe(true);
    expect(isPathWithinRoot(path.join(root, 'a', 'b.txt'), root)).toBe(true);
  });

  it('前缀相似的兄弟目录不算界内（root vs root-other）', () => {
    expect(isPathWithinRoot(`${root}-other`, root)).toBe(false);
    expect(isPathWithinRoot(path.join(`${root}-other`, 'x.txt'), root)).toBe(false);
  });

  it('上级目录与根外路径被拒绝', () => {
    expect(isPathWithinRoot(path.dirname(root), root)).toBe(false);
    expect(isPathWithinRoot(path.resolve(os.tmpdir(), 'elsewhere'), root)).toBe(false);
  });

  it('buildProjectRootViolation 消息包含两个路径与引导语', () => {
    const msg = buildProjectRootViolation('/a/b.txt', '/a');
    expect(msg).toContain('路径越界');
    expect(msg).toContain('/a/b.txt');
    expect(msg).toContain('/a');
    expect(msg).toContain('项目目录');
  });
});

describe('路径围栏 · read/write 集成', () => {
  async function withRootAndOutside(
    run: (paths: { root: string; outside: string; third: string }) => Promise<void>,
  ): Promise<void> {
    const root = await mkdtemp(path.join(os.tmpdir(), 'fence-root-'));
    const outside = await mkdtemp(path.join(os.tmpdir(), 'fence-out-'));
    const third = await mkdtemp(path.join(os.tmpdir(), 'fence-third-'));
    try {
      await run({ root, outside, third });
    } finally {
      await rm(root, { recursive: true, force: true });
      await rm(outside, { recursive: true, force: true });
      await rm(third, { recursive: true, force: true });
    }
  }

  it('read：界内成功；越界拒绝且消息引导回项目目录', async () => {
    await withRootAndOutside(async ({ root, outside }) => {
      const insideFile = path.join(root, 'in.txt');
      const outsideFile = path.join(outside, 'out.txt');
      await writeFile(insideFile, 'in', 'utf-8');
      await writeFile(outsideFile, 'out', 'utf-8');
      const executor = new LocalExecutor();

      const ok = await executor.readLocalFile({ path: insideFile }, root);
      expect(ok.success).toBe(true);
      expect(ok.content).toBe('in');

      const blocked = await executor.readLocalFile({ path: outsideFile }, root);
      expect(blocked.success).toBe(false);
      expect(blocked.error).toContain('路径越界');
      expect(blocked.error).toContain(root);
    });
  });

  it('read：additionalReadRoots 放行 projectRoot 外的指定根，其余界外仍拒绝', async () => {
    await withRootAndOutside(async ({ root, outside, third }) => {
      const extraFile = path.join(outside, 'extra.txt');
      const thirdFile = path.join(third, 'third.txt');
      await writeFile(extraFile, 'extra', 'utf-8');
      await writeFile(thirdFile, 'third', 'utf-8');
      const executor = new LocalExecutor();

      const allowed = await executor.readLocalFile({ path: extraFile }, root, [outside]);
      expect(allowed.success).toBe(true);
      expect(allowed.content).toBe('extra');

      const blocked = await executor.readLocalFile({ path: thirdFile }, root, [outside]);
      expect(blocked.success).toBe(false);
      expect(blocked.error).toContain('路径越界');
    });
  });

  it('write：越界拒绝；additionalReadRoots 只放宽读、不放宽写', async () => {
    await withRootAndOutside(async ({ root, outside }) => {
      const executor = new LocalExecutor();
      // writeLocalFile 签名没有 additionalReadRoots——读/写围栏不对称是设计：
      // 模型可以读参考目录，但只能写项目内。
      const blocked = await executor.writeLocalFile(
        { path: path.join(outside, 'f.txt'), content: 'x' },
        root,
      );
      expect(blocked.success).toBe(false);
      expect(blocked.error).toContain('路径越界');

      const ok = await executor.writeLocalFile({ path: path.join(root, 'f.txt'), content: 'x' }, root);
      expect(ok.success).toBe(true);
    });
  });

  it('不给 projectRoot：无围栏，任意路径可读写', async () => {
    await withRootAndOutside(async ({ outside }) => {
      const executor = new LocalExecutor();
      const file = path.join(outside, 'free.txt');
      const written = await executor.writeLocalFile({ path: file, content: 'free' });
      expect(written.success).toBe(true);
      const read = await executor.readLocalFile({ path: file });
      expect(read.success).toBe(true);
      expect(read.content).toBe('free');
    });
  });
});

describe('serializeFileOp · 同文件串行队列', () => {
  it('同文件并发整体写：全部成功且版本链完整（串行 ⇒ 自动 CAS 逐次通过）', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'q.txt');
      await writeFile(file, 'v0\n', 'utf-8');
      const executor = new LocalExecutor();
      // 建立读证据：之后每个写缺省 expectedVersion 时回落到 readFileState。
      // 串行队列保证第 N 个写执行时 readFileState 已是第 N-1 个写回写的
      // 新版本，与落盘内容一致——并发不会互相覆盖成冲突。
      await executor.readLocalFile({ path: file });

      const contents = ['c1', 'c2', 'c3', 'c4', 'c5'];
      const results = await Promise.all(
        contents.map((content) => executor.writeLocalFile({ path: file, content })),
      );
      for (const r of results) {
        expect(r.success).toBe(true);
      }
      // 最终内容是其中之一（顺序不定），且版本链没断：再写一次无
      // expectedVersion 仍成功（readFileState 与最终落盘一致）。
      expect(contents).toContain(await readFile(file, 'utf-8'));
      const again = await executor.writeLocalFile({ path: file, content: 'final' });
      expect(again.success).toBe(true);
      expect(await readFile(file, 'utf-8')).toBe('final');
    });
  });

  it('不同文件的写互不影响（各自独立队列）', async () => {
    await withTempDir(async (dir) => {
      const f1 = path.join(dir, 'a.txt');
      const f2 = path.join(dir, 'b.txt');
      const executor = new LocalExecutor();
      const [r1, r2] = await Promise.all([
        executor.writeLocalFile({ path: f1, content: 'a' }),
        executor.writeLocalFile({ path: f2, content: 'b' }),
      ]);
      expect(r1.success).toBe(true);
      expect(r2.success).toBe(true);
      expect(await readFile(f1, 'utf-8')).toBe('a');
      expect(await readFile(f2, 'utf-8')).toBe('b');
    });
  });

  it('失败操作不阻塞同路径后续操作（队列从失败中恢复）', async () => {
    await withTempDir(async (dir) => {
      const nested = path.join(dir, 'no', 'way', 'f.txt');
      const executor = new LocalExecutor();
      const fail = await executor.writeLocalFile({ path: nested, content: 'x' });
      expect(fail.success).toBe(false);
      // 同一路径的下一个操作正常执行（prev.then(fn, fn) 的恢复语义）。
      const ok = await executor.writeLocalFile({ path: nested, content: 'x', createDirs: true });
      expect(ok.success).toBe(true);
      expect(await readFile(nested, 'utf-8')).toBe('x');
    });
  });
});

describe('atomicWrite · 临时文件残留', () => {
  it('写入成功后目录无 .tmp- 残留', async () => {
    await withTempDir(async (dir) => {
      const file = path.join(dir, 'f.txt');
      const res = await new LocalExecutor().writeLocalFile({ path: file, content: 'data' });
      expect(res.success).toBe(true);
      expect(await readdir(dir)).toEqual(['f.txt']);
    });
  });

  it('写入失败（父目录不存在）不产生 .tmp- 残留', async () => {
    await withTempDir(async (dir) => {
      const nested = path.join(dir, 'missing', 'f.txt');
      const res = await new LocalExecutor().writeLocalFile({ path: nested, content: 'x' });
      expect(res.success).toBe(false);
      expect(await readdir(dir)).toEqual([]);
    });
  });
});

describe('openLocalTarget · URL/路径分流（mock 系统打开边界）', () => {
  beforeEach(() => {
    mocks.shellOpenExternal.mockReset().mockResolvedValue(undefined);
    mocks.shellOpenPath.mockReset().mockResolvedValue('');
  });

  it('空 target / 纯空白 target 拒绝', async () => {
    const res = await new LocalExecutor().openLocalTarget({ target: '   ' });
    expect(res.success).toBe(false);
    expect(res.error).toBe('target is required');
    expect(mocks.shellOpenExternal).not.toHaveBeenCalled();
    expect(mocks.shellOpenPath).not.toHaveBeenCalled();
  });

  it('http(s) URL 走 shellOpenExternal，原样传递', async () => {
    const res = await new LocalExecutor().openLocalTarget({ target: 'https://example.com/docs' });
    expect(res.success).toBe(true);
    expect(mocks.shellOpenExternal).toHaveBeenCalledWith('https://example.com/docs');
    expect(mocks.shellOpenPath).not.toHaveBeenCalled();
  });

  it('HTML 实体先经 he 解码：&amp; → &', async () => {
    const res = await new LocalExecutor().openLocalTarget({
      target: 'https://example.com/?a=1&amp;b=2',
    });
    expect(res.success).toBe(true);
    expect(mocks.shellOpenExternal).toHaveBeenCalledWith('https://example.com/?a=1&b=2');
  });

  it('maps://maps.apple.com 规范化为 https:// 再打开', async () => {
    const res = await new LocalExecutor().openLocalTarget({
      target: 'maps://maps.apple.com/?q=coffee',
    });
    expect(res.success).toBe(true);
    expect(mocks.shellOpenExternal).toHaveBeenCalledWith('https://maps.apple.com/?q=coffee');
  });

  it('本地路径走 shellOpenPath，`~` 展开；打开失败透出错误消息', async () => {
    mocks.shellOpenPath.mockResolvedValue('boom');
    const res = await new LocalExecutor().openLocalTarget({ target: '~/Documents' });
    expect(res.success).toBe(false);
    expect(res.error).toBe('boom');
    expect(mocks.shellOpenPath).toHaveBeenCalledWith(path.join(os.homedir(), 'Documents'));
    expect(mocks.shellOpenExternal).not.toHaveBeenCalled();
  });

  it('shellOpenExternal 抛错映射为 success=false', async () => {
    mocks.shellOpenExternal.mockRejectedValue(new Error('no browser'));
    const res = await new LocalExecutor().openLocalTarget({ target: 'https://example.com' });
    expect(res.success).toBe(false);
    expect(res.error).toBe('no browser');
  });
});
