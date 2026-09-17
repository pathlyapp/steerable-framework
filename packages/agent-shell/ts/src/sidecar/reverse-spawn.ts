/**
 * W4.1.1 reverse channel: serve the sidecar's `host.process.spawn` by
 * spawning the command confined on Windows via the win-spawn-helper Rust
 * binary (restricted token + Job Object; contract: steerable-framework
 * docs/spec/safety.md "Host capability surface").
 *
 * Fail closed by construction: on non-Windows platforms (which have local
 * rewriter backends and never legitimately route here) or when the helper
 * binary is absent, the handler throws — the sidecar surfaces a tool error
 * and the command never runs unconfined.
 *
 * Enforcement honesty: the helper confines filesystem writes only. When the
 * sidecar policy also constrains the network (`network: false` or an
 * allow-list), the helper's exit frame lists those fields under
 * `sandbox.notEnforced` and this handler reports `partial`.
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';
import log from 'electron-log';
import type { SidecarReverseHandler } from './types.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/** Helper spawn supervision cap; the sidecar's own tool timeout is shorter. */
const HELPER_TIMEOUT_MS = 30 * 60 * 1000;

interface SpawnPolicy {
  writableRoots?: string[];
  network?: boolean;
  allowedHosts?: string[];
}

interface SpawnParams {
  command?: string;
  cwd?: string;
  policy?: SpawnPolicy;
}

export function resolveWinSpawnHelperPath(): string | null {
  const binary = 'win-spawn-helper.exe';
  // 显式覆盖（测试/调试逃生门）：设置后只认这一条路径。指向不存在的
  // 路径即可强制走"helper 缺失"的失败分支，与 dev checkout 是否已构建
  // Rust 二进制无关，保证失败分支的测试确定性。
  const override =
    process.env.DEEPPATH_WIN_SPAWN_HELPER ?? process.env.STEERABLE_WIN_SPAWN_HELPER;
  if (override !== undefined) {
    return override && existsSync(override) ? override : null;
  }
  const resourcesPath = (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
  const candidates: string[] = [];
  if (resourcesPath) {
    candidates.push(join(resourcesPath, 'win-spawn-helper', binary));
  }
  // Host dev checkout (cwd = app repo): native/ copies into resources/.
  // Shell is a published package, so __dirname is inside node_modules and
  // cannot see the host's extraResources.
  candidates.push(join(process.cwd(), 'resources', 'windows-spawn-helper', binary));
  // Legacy: when this file lived in the same repo as the helper.
  candidates.push(join(__dirname, '..', '..', 'resources', 'windows-spawn-helper', binary));
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

export function createProcessSpawnHandler(): SidecarReverseHandler {
  return async (params) => {
    if (process.platform !== 'win32') {
      throw new Error(
        'host.process.spawn is the no-rewriter route for Windows; ' +
          `this host is ${process.platform} — the sidecar should have rewritten the command locally`,
      );
    }
    const p = (params ?? {}) as SpawnParams;
    const command = typeof p.command === 'string' ? p.command : '';
    if (!command.trim()) {
      throw new Error('host.process.spawn: missing command');
    }
    const helper = resolveWinSpawnHelperPath();
    if (!helper) {
      throw new Error(
        'win-spawn-helper.exe not found (packaged resource or resources/windows-spawn-helper); ' +
          'refusing to run the command unconfined',
      );
    }
    const cwd = typeof p.cwd === 'string' && p.cwd ? p.cwd : process.cwd();
    const policy = p.policy ?? {};

    return await runHelper(helper, {
      command,
      cwd,
      writable_roots: policy.writableRoots ?? [],
      network: policy.network ?? false,
      allowed_hosts: policy.allowedHosts ?? [],
    });
  };
}

interface HelperSpec {
  command: string;
  cwd: string;
  writable_roots: string[];
  network: boolean;
  allowed_hosts: string[];
}

function runHelper(
  helperPath: string,
  spec: HelperSpec,
): Promise<{
  exitCode: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  sandbox: { backend: string; enforcement: 'full' | 'partial' };
}> {
  return new Promise((resolve, reject) => {
    const child = spawn(helperPath, [], {
      stdio: ['pipe', 'pipe', 'inherit'],
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    let truncated = false;
    let timedOut = false;
    let settled = false;

    const timer = setTimeout(() => {
      if (settled) return;
      timedOut = true;
      log.warn('[host-spawn] helper timeout; tree-killing confined child');
      try {
        child.stdin.write('{"kill":true}\n');
      } catch {
        // stdin already closed — the helper is gone; destroy below.
      }
      setTimeout(() => {
        if (!settled) child.kill();
      }, 5000).unref();
    }, HELPER_TIMEOUT_MS);
    timer.unref();

    const lines = createInterface({ input: child.stdout });
    lines.on('line', (line) => {
      let frame: {
        type?: string;
        data?: string;
        code?: number;
        stdoutTruncated?: boolean;
        stderrTruncated?: boolean;
        sandbox?: { backend?: string; notEnforced?: string[] };
        message?: string;
      };
      try {
        frame = JSON.parse(line);
      } catch {
        log.warn('[host-spawn] unparseable helper frame', line.slice(0, 200));
        return;
      }
      if (frame.type === 'stdout') {
        stdout += frame.data ?? '';
      } else if (frame.type === 'stderr') {
        stderr += frame.data ?? '';
      } else if (frame.type === 'error') {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`win-spawn-helper setup failed: ${frame.message ?? 'unknown'}`));
      } else if (frame.type === 'exit') {
        settled = true;
        clearTimeout(timer);
        truncated = Boolean(frame.stdoutTruncated || frame.stderrTruncated);
        const notEnforced = frame.sandbox?.notEnforced ?? [];
        resolve({
          exitCode: typeof frame.code === 'number' ? frame.code : 1,
          stdout,
          stderr: timedOut
            ? `${stderr}\n[killed by host: exceeded ${HELPER_TIMEOUT_MS / 60_000}min spawn cap]`
            : stderr,
          truncated,
          sandbox: {
            backend: frame.sandbox?.backend ?? 'windows-restricted-token',
            enforcement: notEnforced.length === 0 ? 'full' : 'partial',
          },
        });
      }
    });

    child.on('error', (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(`failed to spawn win-spawn-helper: ${err.message}`));
    });
    child.on('exit', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      // Helper exited without an exit frame: crash between spawn and report.
      reject(new Error(`win-spawn-helper exited prematurely (code ${code})`));
    });

    child.stdin.write(JSON.stringify(spec) + '\n');
  });
}
