/**
 * collectTurnFiles（回合产物文件收集）测试。
 *
 * 真实 fs + 临时目录，不 mock：扫描器本身就是文件系统边界。时间线靠
 * 「先建旧文件 → 记下 sinceMs → 再动新文件」排开，mtime 用 appendFile
 * 真实推进（不手设 utimes，避免与 birthtime 语义打架）。
 *
 * kind 标签依赖文件系统 birthtime 支持：支持时新建 = 'created'；不支持
 * （birthtimeMs = 0）时降级为 'modified'——用例按运行环境的能力断言。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import { collectTurnFiles } from '../../src/local-backend/turn-files.js';

let root: string;

/** 回合开始时间戳；beforeEach 在建完「旧文件」之前推进，用例各自再定。 */
async function tick(): Promise<number> {
  // 拉开一个可分辨的 mtime 间隔（低端文件系统粒度 1ms 起步）。
  await new Promise((resolve) => setTimeout(resolve, 15));
  return Date.now();
}

/** 该运行环境的文件系统是否暴露可靠 birthtime（决定 kind 断言的期望值）。 */
async function supportsBirthtime(file: string): Promise<boolean> {
  const stat = await fs.stat(file);
  return stat.birthtimeMs > 0;
}

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-'));
});

afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

describe('collectTurnFiles 工作区扫描', () => {
  it('回合内新建的文件被收集，kind 按 birthtime 能力标 created/modified', async () => {
    const sinceMs = await tick();
    const created = path.join(root, '自我介绍.pptx');
    await fs.writeFile(created, 'ppt-bytes');

    const files = await collectTurnFiles({ roots: [root], sinceMs });

    expect(files).toHaveLength(1);
    expect(files[0].path).toBe(created);
    expect(files[0].size).toBe(9);
    expect(files[0].kind).toBe((await supportsBirthtime(created)) ? 'created' : 'modified');
  });

  it('回合前已存在、回合内被修改的文件标 modified', async () => {
    const existing = path.join(root, 'README.md');
    await fs.writeFile(existing, 'old');
    const sinceMs = await tick();
    await fs.appendFile(existing, '-new');

    const files = await collectTurnFiles({ roots: [root], sinceMs });

    expect(files).toEqual([
      { path: existing, kind: 'modified', size: 7 },
    ]);
  });

  it('回合前存在且未触碰的文件不出现', async () => {
    await fs.writeFile(path.join(root, 'old.txt'), 'stale');
    const sinceMs = await tick();

    const files = await collectTurnFiles({ roots: [root], sinceMs });

    expect(files).toEqual([]);
  });

  it('忽略目录（node_modules / .git / .steerable）里的新文件不出现', async () => {
    const sinceMs = await tick();
    for (const dir of ['node_modules', '.git', '.steerable']) {
      await fs.mkdir(path.join(root, dir), { recursive: true });
      await fs.writeFile(path.join(root, dir, 'noise.js'), 'x');
    }
    await fs.writeFile(path.join(root, 'real.txt'), 'y');

    const files = await collectTurnFiles({ roots: [root], sinceMs });

    expect(files.map((f) => f.path)).toEqual([path.join(root, 'real.txt')]);
  });

  it('不跟随符号链接（指到根外的目录不被扫）', async () => {
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-outside-'));
    try {
      const sinceMs = await tick();
      await fs.writeFile(path.join(outside, 'leak.txt'), 'x');
      await fs.symlink(outside, path.join(root, 'linked'));

      const files = await collectTurnFiles({ roots: [root], sinceMs });

      expect(files).toEqual([]);
    } finally {
      await fs.rm(outside, { recursive: true, force: true });
    }
  });

  it('多个根的结果合并并按路径排序', async () => {
    const other = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-pack-'));
    try {
      const sinceMs = await tick();
      const b = path.join(other, 'b.md');
      const a = path.join(root, 'a.md');
      await fs.writeFile(b, 'b');
      await fs.writeFile(a, 'a');

      const files = await collectTurnFiles({ roots: [root, other], sinceMs });

      expect(files.map((f) => f.path)).toEqual([a, b].sort((x, y) => x.localeCompare(y)));
    } finally {
      await fs.rm(other, { recursive: true, force: true });
    }
  });

  it('扫描窗口内被删掉的文件不出现', async () => {
    const sinceMs = await tick();
    const gone = path.join(root, 'gone.txt');
    await fs.writeFile(gone, 'x');
    await fs.rm(gone);

    const files = await collectTurnFiles({ roots: [root], sinceMs });

    expect(files).toEqual([]);
  });
});

describe('collectTurnFiles 写工具参数并集', () => {
  it('可写根之外的 local_write_file 产物经参数并集进入列表', async () => {
    // 扫描根是子目录；写工具落盘到根之外（「完整权限」模式的 Downloads 场景）。
    const sub = path.join(root, 'project');
    await fs.mkdir(sub);
    const outsideFile = path.join(root, 'downloads-report.pdf');
    const sinceMs = await tick();
    await fs.writeFile(outsideFile, 'pdf');

    const files = await collectTurnFiles({
      roots: [sub],
      sinceMs,
      actions: [
        { tool: 'local_write_file', arguments: { path: outsideFile }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([outsideFile]);
  });

  it('相对路径参数按 projectRoot 解析', async () => {
    const sinceMs = await tick();
    const rel = path.join(root, 'out', 'result.txt');
    await fs.mkdir(path.dirname(rel), { recursive: true });
    await fs.writeFile(rel, 'r');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      projectRoot: root,
      actions: [
        { tool: 'local_edit_file', arguments: { path: 'out/result.txt' }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([rel]);
  });

  it('success === false 的写调用不进列表', async () => {
    const sinceMs = await tick();
    const target = path.join(root, 'failed.txt');
    await fs.writeFile(target, 'partial');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_write_file', arguments: { path: target }, success: false },
      ],
    });

    expect(files).toEqual([]);
  });

  it('只读工具（local_read_file 等）的参数不进列表', async () => {
    const sinceMs = await tick();
    const target = path.join(root, 'read.txt');
    await fs.writeFile(target, 'r');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_read_file', arguments: { path: target }, success: true },
      ],
    });

    expect(files).toEqual([]);
  });

  it('写工具指向的文件已不存在时跳过', async () => {
    const sinceMs = await tick();
    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_write_file', arguments: { path: path.join(root, 'ghost.txt') }, success: true },
      ],
    });

    expect(files).toEqual([]);
  });

  it('扫描与参数并集按路径去重', async () => {
    const sinceMs = await tick();
    const target = path.join(root, 'dup.txt');
    await fs.writeFile(target, 'd');

    const files = await collectTurnFiles({
      roots: [root],
      sinceMs,
      actions: [
        { tool: 'local_write_file', arguments: { path: target }, success: true },
      ],
    });

    expect(files).toHaveLength(1);
  });
});

describe('collectTurnFiles exec cwd 浅扫描', () => {
  it('显式 cwd 在递归根之外：顶层新文件被收集，子目录与点文件被跳过', async () => {
    // 「完整权限」/无围栏场景：脚本在 cwd 落盘，cwd 不在任何递归根里。
    const proj = path.join(root, 'project');
    const work = path.join(root, 'work');
    await fs.mkdir(proj);
    await fs.mkdir(path.join(work, 'sub'), { recursive: true });
    const sinceMs = await tick();
    const top = path.join(work, '报告.pptx');
    await fs.writeFile(top, 'ppt');
    await fs.writeFile(path.join(work, 'sub', 'nested.pptx'), 'ppt');
    await fs.writeFile(path.join(work, '.hidden'), 'x');

    const files = await collectTurnFiles({
      roots: [proj],
      sinceMs,
      actions: [
        { tool: 'local_exec_shell', arguments: { command: 'python3 gen.py', cwd: work }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([top]);
  });

  it('缺省 cwd + 无项目对话：回落到 home 顶层（无项目 exec 产物最常见的落点）', async () => {
    const home = path.join(root, 'fake-home');
    await fs.mkdir(home);
    const sinceMs = await tick();
    const ppt = path.join(home, '自我介绍_张三.pptx');
    await fs.writeFile(ppt, 'ppt');
    await fs.writeFile(path.join(home, '.zsh_history'), 'noise');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      homeDir: home,
      actions: [
        { tool: 'local_exec_shell', arguments: { command: 'python3 gen_ppt.py' }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([ppt]);
  });

  it('缺省 cwd + 有项目：回落到项目根，由递归扫描覆盖（不重复）', async () => {
    const proj = path.join(root, 'project');
    await fs.mkdir(proj);
    const sinceMs = await tick();
    const out = path.join(proj, 'out.txt');
    await fs.writeFile(out, 'o');

    const files = await collectTurnFiles({
      roots: [proj],
      sinceMs,
      projectRoot: proj,
      actions: [
        { tool: 'local_run_snippet', arguments: { language: 'python', code: 'open("out.txt","w")' }, success: true },
      ],
    });

    expect(files).toHaveLength(1);
    expect(files[0].path).toBe(out);
  });

  it('非 exec 工具的 cwd 字段不触发浅扫描', async () => {
    const work = path.join(root, 'work');
    await fs.mkdir(work);
    const sinceMs = await tick();
    await fs.writeFile(path.join(work, 'x.txt'), 'x');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_read_file', arguments: { path: work, cwd: work }, success: true },
      ],
    });

    expect(files).toEqual([]);
  });
});

describe('collectTurnFiles 命令文本路径字面量', () => {
  it('python 代码里引号包裹的绝对路径（prs.save 场景）被收集', async () => {
    // 产物写到与 cwd 无关的位置：只有命令文本里出现过这个路径。
    const cwd = path.join(root, 'cwd');
    const elsewhere = path.join(root, 'elsewhere');
    await fs.mkdir(cwd);
    await fs.mkdir(elsewhere);
    const sinceMs = await tick();
    const ppt = path.join(elsewhere, '自我介绍_张三.pptx');
    await fs.writeFile(ppt, 'ppt');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        {
          tool: 'local_exec_shell',
          arguments: { command: `python3 -c "from pptx import Presentation; prs.save('${ppt}')"`, cwd },
          success: true,
        },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([ppt]);
  });

  it('裸路径（shell 重定向）与 run_snippet 的 code 字段都被提取', async () => {
    const cwd = path.join(root, 'cwd');
    await fs.mkdir(cwd);
    const sinceMs = await tick();
    const csv = path.join(root, 'report.csv');
    const png = path.join(root, 'chart.png');
    await fs.writeFile(csv, 'a,b');
    await fs.writeFile(png, 'png');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_exec_shell', arguments: { command: `python3 gen.py > ${csv}`, cwd }, success: true },
        { tool: 'local_run_snippet', arguments: { language: 'python', code: `fig.savefig("${png}")`, cwd }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([csv, png].sort((a, b) => a.localeCompare(b)));
  });

  it('~/ 前缀展开到 home 目录', async () => {
    const home = path.join(root, 'fake-home');
    const cwd = path.join(root, 'cwd');
    await fs.mkdir(home);
    await fs.mkdir(cwd);
    const sinceMs = await tick();
    const doc = path.join(home, 'notes.md');
    await fs.writeFile(doc, 'n');

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      homeDir: home,
      actions: [
        { tool: 'local_exec_shell', arguments: { command: 'python3 gen.py --out ~/notes.md', cwd }, success: true },
      ],
    });

    expect(files.map((f) => f.path)).toEqual([doc]);
  });

  it('命令里提到的旧文件（回合前存在且未动）不被误收', async () => {
    const cwd = path.join(root, 'cwd');
    await fs.mkdir(cwd);
    const stale = path.join(root, 'stale.pptx');
    await fs.writeFile(stale, 'old');
    const sinceMs = await tick();

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        { tool: 'local_exec_shell', arguments: { command: `ls -la ${stale}`, cwd }, success: true },
      ],
    });

    expect(files).toEqual([]);
  });

  it('不存在的路径与指向目录的字面量都被 stat 把关跳过', async () => {
    const cwd = path.join(root, 'cwd');
    const bundle = path.join(root, 'Demo.app');
    await fs.mkdir(cwd);
    await fs.mkdir(bundle);
    const sinceMs = await tick();

    const files = await collectTurnFiles({
      roots: [],
      sinceMs,
      actions: [
        {
          tool: 'local_exec_shell',
          arguments: {
            command: `open ${bundle} && ls ${path.join(root, 'ghost.pdf')}`,
            cwd,
          },
          success: true,
        },
      ],
    });

    expect(files).toEqual([]);
  });
});
