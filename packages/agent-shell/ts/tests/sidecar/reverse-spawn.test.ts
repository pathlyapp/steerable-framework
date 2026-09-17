/**
 * reverse-spawn fail-closed contract (W4.1.1): the handler must never let a
 * command run unconfined — wrong platform or missing helper both throw, and
 * the sidecar turns that into a tool error. The confined-spawn behavior
 * itself is proven by the helper's Rust integration tests on the Windows CI
 * runner (test-windows-spawn.yml); the frame protocol is covered there from
 * the helper side.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  createProcessSpawnHandler,
  resolveWinSpawnHelperPath,
} from '../../src/sidecar/reverse-spawn.js';

type ElectronProcess = NodeJS.Process & {
  resourcesPath?: string;
  defaultApp?: boolean;
};

describe('host.process.spawn handler', () => {
  it('fails closed on non-Windows platforms', async () => {
    if (process.platform === 'win32') return;
    const handler = createProcessSpawnHandler();
    await expect(
      handler({ command: 'echo hi', policy: { writableRoots: [] } }),
    ).rejects.toThrow(/only implemented|no-rewriter route/);
  });

  it('fails closed when the helper binary is absent', async () => {
    if (process.platform !== 'win32') return;
    // 用 DEEPPATH_WIN_SPAWN_HELPER 指向一个保证不存在的路径，强制走缺失
    // 分支——与 dev checkout 是否已构建 Rust 二进制无关，保持测试确定性。
    const prev = process.env.DEEPPATH_WIN_SPAWN_HELPER;
    process.env.DEEPPATH_WIN_SPAWN_HELPER = join(
      tmpdir(),
      'definitely-missing-win-spawn-helper.exe',
    );
    try {
      const handler = createProcessSpawnHandler();
      await expect(
        handler({ command: 'echo hi', policy: { writableRoots: [] } }),
      ).rejects.toThrow(/win-spawn-helper\.exe not found/);
    } finally {
      if (prev === undefined) delete process.env.DEEPPATH_WIN_SPAWN_HELPER;
      else process.env.DEEPPATH_WIN_SPAWN_HELPER = prev;
    }
  });

  it('runs confined when the helper binary is present', async () => {
    if (process.platform !== 'win32') return;
    // helper 只在打包构建（cargo build）后存在于 dev checkout；未构建时跳过，
    // 受限 spawn 的完整行为由 Rust 侧集成测试覆盖（test-windows-spawn.yml）。
    if (!resolveWinSpawnHelperPath()) return;
    const handler = createProcessSpawnHandler();
    const result = (await handler({
      command: 'echo hi',
      policy: { writableRoots: [] },
    })) as { exitCode?: number; stdout?: string; sandbox?: { backend?: string } };
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toContain('hi');
    expect(result.sandbox?.backend).toBe('windows-restricted-token');
  });

  it('rejects an empty command before touching the helper', async () => {
    if (process.platform !== 'win32') return;
    const handler = createProcessSpawnHandler();
    await expect(handler({ command: '  ' })).rejects.toThrow(/missing command/);
  });
});

describe('resolveWinSpawnHelperPath', () => {
  const electron = process as ElectronProcess;
  const envKeys = ['DEEPPATH_WIN_SPAWN_HELPER', 'STEERABLE_WIN_SPAWN_HELPER'] as const;
  let scratch = '';
  let prevCwd = '';
  let prevResourcesPath: string | undefined;
  let prevDefaultApp: boolean | undefined;
  const prevEnv: Record<(typeof envKeys)[number], string | undefined> = {
    DEEPPATH_WIN_SPAWN_HELPER: undefined,
    STEERABLE_WIN_SPAWN_HELPER: undefined,
  };

  beforeEach(() => {
    scratch = mkdtempSync(join(tmpdir(), 'win-spawn-helper-resolve-'));
    prevCwd = process.cwd();
    prevResourcesPath = electron.resourcesPath;
    prevDefaultApp = electron.defaultApp;
    for (const key of envKeys) prevEnv[key] = process.env[key];
    delete process.env.DEEPPATH_WIN_SPAWN_HELPER;
    delete process.env.STEERABLE_WIN_SPAWN_HELPER;
    delete electron.resourcesPath;
    delete electron.defaultApp;
  });

  afterEach(() => {
    process.chdir(prevCwd);
    if (prevResourcesPath === undefined) delete electron.resourcesPath;
    else electron.resourcesPath = prevResourcesPath;
    if (prevDefaultApp === undefined) delete electron.defaultApp;
    else electron.defaultApp = prevDefaultApp;
    for (const key of envKeys) {
      if (prevEnv[key] === undefined) delete process.env[key];
      else process.env[key] = prevEnv[key];
    }
    rmSync(scratch, { recursive: true, force: true });
  });

  function touch(root: string, relativeDir: string): string {
    const full = join(root, relativeDir, 'win-spawn-helper.exe');
    mkdirSync(join(full, '..'), { recursive: true });
    writeFileSync(full, '');
    return full;
  }

  it('honors STEERABLE_WIN_SPAWN_HELPER when DEEPPATH_ is unset', () => {
    const helper = touch(scratch, 'override');
    process.env.STEERABLE_WIN_SPAWN_HELPER = helper;
    expect(resolveWinSpawnHelperPath()).toBe(helper);
  });

  it('prefers DEEPPATH_WIN_SPAWN_HELPER over STEERABLE_WIN_SPAWN_HELPER', () => {
    const deeppath = touch(scratch, 'deeppath');
    const steerable = touch(scratch, 'steerable');
    process.env.DEEPPATH_WIN_SPAWN_HELPER = deeppath;
    process.env.STEERABLE_WIN_SPAWN_HELPER = steerable;
    expect(resolveWinSpawnHelperPath()).toBe(deeppath);
  });

  it('returns null when the override path is missing', () => {
    process.env.STEERABLE_WIN_SPAWN_HELPER = join(scratch, 'missing', 'win-spawn-helper.exe');
    expect(resolveWinSpawnHelperPath()).toBeNull();
  });

  it('finds the host checkout helper under cwd/resources', () => {
    touch(scratch, join('resources', 'windows-spawn-helper'));
    process.chdir(scratch);
    expect(resolveWinSpawnHelperPath()).toBe(
      join(process.cwd(), 'resources', 'windows-spawn-helper', 'win-spawn-helper.exe'),
    );
  });

  it('finds the helper via cwd when running unpackaged Electron', () => {
    touch(scratch, join('resources', 'windows-spawn-helper'));
    electron.resourcesPath = join(scratch, 'electron-resources');
    electron.defaultApp = true;
    process.chdir(scratch);
    expect(resolveWinSpawnHelperPath()).toBe(
      join(process.cwd(), 'resources', 'windows-spawn-helper', 'win-spawn-helper.exe'),
    );
  });

  it('prefers packaged extraResources over cwd', () => {
    const packaged = touch(join(scratch, 'packaged'), 'win-spawn-helper');
    touch(scratch, join('resources', 'windows-spawn-helper'));
    electron.resourcesPath = join(scratch, 'packaged');
    process.chdir(scratch);
    expect(resolveWinSpawnHelperPath()).toBe(packaged);
  });

  it('does not take a cwd helper in a packaged Electron app', () => {
    touch(scratch, join('resources', 'windows-spawn-helper'));
    electron.resourcesPath = join(scratch, 'packaged');
    process.chdir(scratch);
    expect(resolveWinSpawnHelperPath()).toBeNull();
  });
});
