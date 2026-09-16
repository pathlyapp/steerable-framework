/**
 * reverse-spawn fail-closed contract (W4.1.1): the handler must never let a
 * command run unconfined — wrong platform or missing helper both throw, and
 * the sidecar turns that into a tool error. The confined-spawn behavior
 * itself is proven by the helper's Rust integration tests on the Windows CI
 * runner (test-windows-spawn.yml); the frame protocol is covered there from
 * the helper side.
 */

import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  createProcessSpawnHandler,
  resolveWinSpawnHelperPath,
} from '../../src/sidecar/reverse-spawn.js';

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
