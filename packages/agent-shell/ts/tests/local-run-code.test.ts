import { describe, expect, it } from 'vitest';
import { execSync } from 'node:child_process';
import { LocalExecutor } from '../src/local-executor.js';
import { ToolRouter } from '../src/tool-router.js';

const hasPython = (() => {
  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`${cmd} --version`, { stdio: 'ignore' });
      return true;
    } catch {
      /* try next */
    }
  }
  return false;
})();

const hasNode = (() => {
  try {
    execSync('node --version', { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
})();

describe('LocalExecutor.runCode', () => {
  it('拒绝空 code 与不支持的 language', async () => {
    const executor = new LocalExecutor();
    const empty = await executor.runCode({ language: 'python', code: '   ' });
    expect(empty.success).toBe(false);
    const badLang = await executor.runCode({ language: 'ruby', code: 'puts 1' });
    expect(badLang.success).toBe(false);
    expect(badLang.error).toContain('不支持的语言');
  });

  it.skipIf(!hasPython)('python 片段：真实运行并返回 stdout / scriptPath / interpreter', async () => {
    const executor = new LocalExecutor();
    const result = await executor.runCode({
      language: 'python',
      code: 'print(1 + 1)',
      timeout: 15_000,
    });
    expect(result.success).toBe(true);
    expect(result.exitCode).toBe(0);
    expect(result.stdout?.trim()).toBe('2');
    expect(result.language).toBe('python');
    expect(result.interpreter).toBeTruthy();
    expect(result.scriptPath).toMatch(/\.py$/);
  });

  it.skipIf(!hasPython)('python 片段：非 0 退出码时透出 stderr', async () => {
    const executor = new LocalExecutor();
    const result = await executor.runCode({
      language: 'python',
      code: 'import sys; sys.stderr.write("boom\\n"); sys.exit(3)',
      timeout: 15_000,
    });
    expect(result.success).toBe(false);
    expect(result.exitCode).toBe(3);
    expect(result.stderr).toContain('boom');
  });

  it.skipIf(!hasNode)('node 片段：真实运行并返回 stdout', async () => {
    const executor = new LocalExecutor();
    const result = await executor.runCode({
      language: 'node',
      code: 'console.log(2 * 21)',
      timeout: 15_000,
    });
    expect(result.success).toBe(true);
    expect(result.stdout?.trim()).toBe('42');
    expect(result.scriptPath).toMatch(/\.js$/);
  });

  it('非法依赖包名被拒绝（防注入）', async () => {
    const executor = new LocalExecutor();
    const pip = await executor.runCode({
      language: 'python',
      code: 'print(1)',
      pipPackages: ['requests"; rm -rf ~; "'],
    });
    expect(pip.success).toBe(false);
    expect(pip.error).toContain('非法 pip 包名');
    const npm = await executor.runCode({
      language: 'node',
      code: 'console.log(1)',
      npmPackages: ['xlsx; echo pwned'],
    });
    expect(npm.success).toBe(false);
    expect(npm.error).toContain('非法 npm 包名');
  });
});

describe('ToolRouter / local_run_snippet', () => {
  function makeRouter(runCode: LocalExecutor['runCode']) {
    return new ToolRouter(
      {
        executeShell: async () => ({ success: true }),
        readLocalFile: async () => ({ success: true, content: '' }),
        writeLocalFile: async () => ({ success: true }),
        openLocalTarget: async () => ({ success: true }),
        runCode,
      } as never,
      { list: () => [], getById: () => null } as never,
    );
  }

  it('schema 出场且为 destructive（plan 模式只读过滤会排掉它）', () => {
    const router = makeRouter(async () => ({ success: true }));
    const schema = router.getSchemaByName('local_run_snippet');
    expect(schema).toBeTruthy();
    expect(schema?.mode).toBe('destructive');
    expect(schema?.inputSchema).toMatchObject({ required: ['language', 'code'] });
  });

  it('参数透传：language/code/pipPackages/npmPackages 到达 executor', async () => {
    let seen: Record<string, unknown> | null = null;
    const router = makeRouter(async (req) => {
      seen = req as unknown as Record<string, unknown>;
      return { success: true };
    });
    await router.execute({
      name: 'local_run_snippet',
      arguments: {
        language: 'python',
        code: 'print(1)',
        pipPackages: ['pandas'],
        npmPackages: 'not-an-array',
      },
    });
    expect(seen).toMatchObject({
      language: 'python',
      code: 'print(1)',
      pipPackages: ['pandas'],
    });
    expect(seen!.npmPackages).toBeUndefined();
  });

  it('项目沙箱：未给 cwd 时默认项目根；显式越界 cwd 被拒绝', async () => {
    let seenCwd: string | undefined;
    const router = makeRouter(async (req) => {
      seenCwd = req.cwd;
      return { success: true };
    });
    const root = process.platform === 'win32' ? 'C:\\proj\\a' : '/tmp/proj-a';
    await router.execute(
      { name: 'local_run_snippet', arguments: { language: 'node', code: '1' } },
      { projectRoot: root },
    );
    expect(seenCwd).toBe(root);

    const outside = process.platform === 'win32' ? 'C:\\other' : '/etc';
    const rejected = await router.execute(
      {
        name: 'local_run_snippet',
        arguments: { language: 'node', code: '1', cwd: outside },
      },
      { projectRoot: root },
    );
    expect((rejected as { success: boolean }).success).toBe(false);
    expect((rejected as { error: string }).error).toContain('路径越界');
  });
});
