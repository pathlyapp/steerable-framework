/**
 * resolveSidecarPython 的打包布局回归测试。
 *
 * build_sidecar.py 产出的 runtime 布局是 <platform>/python/<exe>
 * （Windows: python/python.exe；POSIX: python/bin/python3）。历史上
 * 解析器只找 <platform>/<exe> 和 <platform>/bin/<exe>，在 Windows 打包
 * 产物里双双落空，静默落到系统 python → "No module named
 * steerable_sidecar" 崩溃循环（packaged-smoke 实测发现）。
 */
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { resolveSidecarPython } from '../../src/sidecar/supervisor.js';

describe('resolveSidecarPython packaged runtime layout', () => {
  const platformTag =
    process.platform === 'darwin'
      ? process.arch === 'arm64'
        ? 'darwin-arm64'
        : 'darwin-x64'
      : process.platform === 'win32'
        ? 'win32-x64'
        : 'linux-x64';
  const binaryName = process.platform === 'win32' ? 'python.exe' : 'python3';

  let scratch = '';
  let prevResourcesPath: unknown;
  let prevEnv: string | undefined;

  beforeEach(() => {
    scratch = mkdtempSync(join(tmpdir(), 'python-resolve-'));
    prevResourcesPath = (process as NodeJS.Process & { resourcesPath?: unknown }).resourcesPath;
    prevEnv = process.env.STEERABLE_SIDECAR_PYTHON;
    delete process.env.STEERABLE_SIDECAR_PYTHON;
  });

  afterEach(() => {
    if (prevResourcesPath === undefined) {
      delete (process as NodeJS.Process & { resourcesPath?: unknown }).resourcesPath;
    } else {
      (process as NodeJS.Process & { resourcesPath?: unknown }).resourcesPath = prevResourcesPath;
    }
    if (prevEnv === undefined) delete process.env.STEERABLE_SIDECAR_PYTHON;
    else process.env.STEERABLE_SIDECAR_PYTHON = prevEnv;
    rmSync(scratch, { recursive: true, force: true });
  });

  function touch(relative: string): string {
    const full = join(scratch, 'python-runtime', platformTag, relative);
    mkdirSync(join(full, '..'), { recursive: true });
    writeFileSync(full, '');
    return full;
  }

  it('finds the build_sidecar.py layout (<platform>/python/<exe>) via resourcesPath', () => {
    const expected = touch(join('python', binaryName));
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = scratch;
    expect(resolveSidecarPython()).toBe(expected);
  });

  it('finds the POSIX variant (<platform>/python/bin/python3)', () => {
    if (process.platform === 'win32') return;
    const expected = touch(join('python', 'bin', binaryName));
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = scratch;
    expect(resolveSidecarPython()).toBe(expected);
  });

  it('STEERABLE_SIDECAR_PYTHON still wins over the bundled runtime', () => {
    touch(join('python', binaryName));
    (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath = scratch;
    const override = touch(join('..', '..', 'override-python'));
    process.env.STEERABLE_SIDECAR_PYTHON = override;
    expect(resolveSidecarPython()).toBe(override);
  });
});
