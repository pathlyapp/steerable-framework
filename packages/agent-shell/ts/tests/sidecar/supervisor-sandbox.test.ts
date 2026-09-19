/**
 * Unit tests for the supervisor's process-sandbox spawn wiring — no real
 * subprocess.
 *
 * The integration counterpart (real sandboxed boot) lives in
 * supervisor.integration.test.ts and is opt-in; here we mock spawn/execFile
 * to pin the spawn-plan decisions: opt-out gating, profile / linux-wrap
 * generation through the sidecar package, sandbox-exec / bwrap argv
 * shape, and the refuse-not-fallback paths (profile failure, wrap failure,
 * missing helper, unsupported OS).
 */

import { EventEmitter } from 'node:events';
import { promisify } from 'node:util';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { spawnMock, execFileImpl, seatbelt, winHelper, rustBin } = vi.hoisted(() => ({
  spawnMock: vi.fn(),
  execFileImpl: vi.fn(),
  // Tests run on Linux CI too: the sandboxed-path cases stub their way onto
  // a macOS host where /usr/bin/sandbox-exec exists.
  seatbelt: { exists: true },
  winHelper: { exists: false },
  rustBin: { exists: false, path: '/fake/steerable-sidecar' },
}));

vi.mock('node:child_process', async (importOriginal) => {
  const mod = await importOriginal<typeof import('node:child_process')>();
  const execFileFn: unknown = Object.assign(
    (...args: unknown[]) => {
      // Callback-style fallback (unused by the supervisor).
      const cb = args[args.length - 1];
      if (typeof cb === 'function') cb(new Error('execFile mock: no impl'));
    },
    { [promisify.custom]: (...args: unknown[]) => execFileImpl(...args) },
  );
  return { ...mod, spawn: spawnMock, execFile: execFileFn };
});

vi.mock('node:fs', async (importOriginal) => {
  const mod = await importOriginal<typeof import('node:fs')>();
  return {
    ...mod,
    existsSync: (p: Parameters<typeof mod.existsSync>[0]) => {
      const s = String(p);
      if (s === '/usr/bin/sandbox-exec') return seatbelt.exists;
      if (s.includes('win-spawn-helper')) return winHelper.exists;
      if (s === rustBin.path) return rustBin.exists;
      return mod.existsSync(p);
    },
  };
});

import { SidecarSandboxUnavailableError, SidecarSupervisor } from '../../src/sidecar';

const READY_LINE =
  '__SIDECAR_READY__:{"status":"ok","protocolVersion":"0.1.0","pid":1}\n';

function fakeChild() {
  const child = new EventEmitter() as EventEmitter & {
    stdout: EventEmitter & { setEncoding: (enc: string) => void };
    stderr: EventEmitter & { setEncoding: (enc: string) => void };
    stdin: { write: (data: string, cb?: (err?: Error | null) => void) => void };
    kill: () => void;
  };
  child.stdout = Object.assign(new EventEmitter(), { setEncoding: () => {} });
  child.stderr = Object.assign(new EventEmitter(), { setEncoding: () => {} });
  child.stdin = {
    write: (data, cb) => {
      // Graceful shutdown handshake: respond to the RPC, then exit — on a
      // later macrotask so the supervisor's `await call(...)` continuation
      // registers its exit listener before the event fires.
      if (data.includes('system.shutdown')) {
        let id = 0;
        try {
          id = (JSON.parse(data.trim()) as { id?: number }).id ?? 0;
        } catch {
          /* still exit */
        }
        queueMicrotask(() =>
          child.stdout.emit('data', `{"jsonrpc":"2.0","id":${id},"result":null}\n`),
        );
        setTimeout(() => child.emit('exit', 0, null), 0);
      }
      cb?.();
    },
  };
  child.kill = () => {};
  queueMicrotask(() => child.stderr.emit('data', READY_LINE));
  return child;
}

const PROFILE = '(version 1)\n(deny default)\n(allow network-outbound)\n';
const LINUX_WRAP =
  JSON.stringify({
    argv: ['/usr/bin/bwrap', '--ro-bind', '/', '/', '--', '/fake/python3', '-m', 'steerable_sidecar'],
    backend: 'bwrap',
    enforcement: 'partial',
  }) + '\n';

async function expectRefuse(
  options: Parameters<typeof SidecarSupervisor.start>[0],
  reason: 'platform_unsupported' | 'seatbelt_missing' | 'profile_failed' | 'wrap_failed' | 'helper_missing',
) {
  await expect(
    SidecarSupervisor.start({
      pythonExecutable: '/fake/python3',
      healthIntervalMs: 0,
      ...options,
    }),
  ).rejects.toBeInstanceOf(SidecarSandboxUnavailableError);
  expect(SidecarSupervisor.lastSpawnRefusal).toEqual({
    backend: 'none',
    enforcement: 'none',
    reason,
  });
  expect(spawnMock).not.toHaveBeenCalled();
}

describe('SidecarSupervisor sandbox spawn plan', () => {
  const savedEnv = { ...process.env };
  const realPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!;

  beforeEach(() => {
    spawnMock.mockReset();
    execFileImpl.mockReset();
    spawnMock.mockImplementation(() => fakeChild());
    execFileImpl.mockResolvedValue({ stdout: PROFILE, stderr: '' });
    delete process.env.STEERABLE_SIDECAR_SANDBOX;
    delete process.env.STEERABLE_RUST_SIDECAR;
    delete process.env.STEERABLE_RUST_SIDECAR_BIN;
    SidecarSupervisor.lastSpawnRefusal = null;
    // Default to a Seatbelt-capable macOS host; the off-macOS case restubs.
    Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
    seatbelt.exists = true;
    winHelper.exists = false;
    rustBin.exists = false;
  });

  afterEach(() => {
    process.env = { ...savedEnv };
    Object.defineProperty(process, 'platform', realPlatform);
  });

  async function startAndStop(options: Parameters<typeof SidecarSupervisor.start>[0]) {
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: '/fake/python3',
      healthIntervalMs: 0,
      ...options,
    });
    await supervisor.shutdown();
    return supervisor;
  }

  it('wraps the spawn in sandbox-exec by default (Wave 4 default-on)', async () => {
    await startAndStop({});
    expect(spawnMock).toHaveBeenCalledOnce();
    const [command] = spawnMock.mock.calls[0];
    expect(command).toBe('/usr/bin/sandbox-exec');
    expect(execFileImpl).toHaveBeenCalledOnce();
  });

  it('spawns python directly when sandbox: false', async () => {
    await startAndStop({ sandbox: false });
    expect(spawnMock).toHaveBeenCalledOnce();
    const [command, args] = spawnMock.mock.calls[0];
    expect(command).toBe('/fake/python3');
    expect(args).toEqual(['-m', 'steerable_sidecar']);
    expect(execFileImpl).not.toHaveBeenCalled();
  });

  it('wraps the spawn in sandbox-exec when sandbox: true', async () => {
    await startAndStop({ sandbox: true });
    expect(execFileImpl).toHaveBeenCalledOnce();
    // Profile generation goes through the sidecar package itself.
    const [py, cliArgs] = execFileImpl.mock.calls[0];
    expect(py).toBe('/fake/python3');
    expect(cliArgs.slice(0, 3)).toEqual(['-m', 'steerable_sidecar.sandbox', 'profile']);
    expect(cliArgs).toContain('--writable-root');

    expect(spawnMock).toHaveBeenCalledOnce();
    const [command, args, spawnOptions] = spawnMock.mock.calls[0];
    expect(command).toBe('/usr/bin/sandbox-exec');
    expect(args[0]).toBe('-p');
    expect(args[1]).toContain('(deny default)');
    expect(args.slice(2)).toEqual(['/fake/python3', '-m', 'steerable_sidecar']);
    // The sandbox denies __pycache__ writes; bytecode caching is disabled.
    expect(spawnOptions.env.PYTHONDONTWRITEBYTECODE).toBe('1');
  });

  it('generates the Seatbelt profile from the rust sidecar when the flag is on', async () => {
    process.env.STEERABLE_RUST_SIDECAR = '1';
    rustBin.exists = true;
    await startAndStop({ rustSidecarBin: '/fake/steerable-sidecar', sandbox: true });
    const [bin, cliArgs] = execFileImpl.mock.calls[0];
    expect(bin).toBe('/fake/steerable-sidecar');
    expect(cliArgs.slice(0, 2)).toEqual(['sandbox', 'profile']);
    expect(cliArgs).toContain('--writable-root');
    const [command, args] = spawnMock.mock.calls[0];
    expect(command).toBe('/usr/bin/sandbox-exec');
    expect(args.slice(2)).toEqual(['/fake/steerable-sidecar']);
  });

  it('STEERABLE_SIDECAR_SANDBOX=0 opts out when the option is unset', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX = '0';
    await startAndStop({});
    expect(spawnMock.mock.calls[0][0]).toBe('/fake/python3');
    expect(execFileImpl).not.toHaveBeenCalled();
  });

  it('option true beats the opt-out env var', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX = '0';
    await startAndStop({ sandbox: true });
    expect(spawnMock.mock.calls[0][0]).toBe('/usr/bin/sandbox-exec');
  });

  it('passes no --allow-host flags by default (egress stays open)', async () => {
    await startAndStop({ sandbox: true });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).not.toContain('--allow-host');
  });

  it('forwards sandboxAllowedHosts as --allow-host flags', async () => {
    await startAndStop({
      sandbox: true,
      sandboxAllowedHosts: ['api.deepseek.com', 'localhost:11434'],
    });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).toEqual(
      expect.arrayContaining([
        '--allow-host',
        'api.deepseek.com',
        '--allow-host',
        'localhost:11434',
      ]),
    );
  });

  it('passes --allow-resolver (not --allow-web-egress) in egress-proxy mode (3.1b)', async () => {
    // The proxy holds the host list; the sandbox only needs the resolver
    // socket for web_fetch's SSRF pre-check. Opening *:80/443 here would
    // let the sidecar bypass the proxy entirely.
    await startAndStop({
      sandbox: true,
      sandboxAllowedHosts: ['127.0.0.1:18899'],
      sandboxAllowResolver: true,
    });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).toContain('--allow-resolver');
    expect(cliArgs).not.toContain('--allow-web-egress');
  });

  it('omits --allow-resolver when web egress already grants the resolver', async () => {
    await startAndStop({
      sandbox: true,
      sandboxAllowedHosts: ['api.deepseek.com'],
      sandboxWebEgress: true,
      sandboxAllowResolver: true,
    });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).toContain('--allow-web-egress');
    expect(cliArgs).not.toContain('--allow-resolver');
  });

  it('falls back to STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS (comma-separated)', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS = 'api.openai.com, 127.0.0.1:11434';
    await startAndStop({ sandbox: true });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).toEqual(
      expect.arrayContaining([
        '--allow-host',
        'api.openai.com',
        '--allow-host',
        '127.0.0.1:11434',
      ]),
    );
  });

  it('option beats the env var for the allow-list', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS = 'env-host.example.com';
    await startAndStop({ sandbox: true, sandboxAllowedHosts: [] });
    const [, cliArgs] = execFileImpl.mock.calls[0];
    expect(cliArgs).not.toContain('--allow-host');
  });

  it('option false beats a legacy opt-in env var', async () => {
    process.env.STEERABLE_SIDECAR_SANDBOX = '1';
    await startAndStop({ sandbox: false });
    expect(spawnMock.mock.calls[0][0]).toBe('/fake/python3');
  });

  it('wraps the Linux sidecar in the linux-wrap argv', async () => {
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    execFileImpl.mockResolvedValue({ stdout: LINUX_WRAP, stderr: '' });
    const lines: string[] = [];
    await startAndStop({ sandbox: true, onLogLine: (line) => lines.push(line) });
    const [py, cliArgs] = execFileImpl.mock.calls[0];
    expect(py).toBe('/fake/python3');
    expect(cliArgs.slice(0, 3)).toEqual(['-m', 'steerable_sidecar.sandbox', 'linux-wrap']);
    expect(spawnMock).toHaveBeenCalledOnce();
    const [command, args] = spawnMock.mock.calls[0];
    expect(command).toBe('/usr/bin/bwrap');
    expect(args.slice(-2)).toEqual(['-m', 'steerable_sidecar']);
    expect(lines.some((l) => l.includes('bwrap') && l.includes('active'))).toBe(true);
  });

  it('wraps the Linux rust sidecar via sandbox linux-wrap', async () => {
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    process.env.STEERABLE_RUST_SIDECAR = '1';
    rustBin.exists = true;
    execFileImpl.mockResolvedValue({
      stdout:
        JSON.stringify({
          argv: ['/usr/bin/bwrap', '--ro-bind', '/', '/', '--', '/fake/steerable-sidecar'],
          backend: 'bwrap',
          enforcement: 'partial',
        }) + '\n',
      stderr: '',
    });
    await startAndStop({ rustSidecarBin: '/fake/steerable-sidecar', sandbox: true });
    const [bin, cliArgs] = execFileImpl.mock.calls[0];
    expect(bin).toBe('/fake/steerable-sidecar');
    expect(cliArgs.slice(0, 2)).toEqual(['sandbox', 'linux-wrap']);
    const [command, args] = spawnMock.mock.calls[0];
    expect(command).toBe('/usr/bin/bwrap');
    expect(args.at(-1)).toBe('/fake/steerable-sidecar');
  });

  it('logs the honest egress story on Linux when the proxy holds the host list (3.1c)', async () => {
    // linux-wrap has no per-host pinning: under the egress proxy the
    // per-host enforcement lives in the proxy (HTTPS_PROXY env) plus the
    // sidecar's app-layer domain list — the log must say exactly that.
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    execFileImpl.mockResolvedValue({ stdout: LINUX_WRAP, stderr: '' });
    const lines: string[] = [];
    await startAndStop({
      sandbox: true,
      env: { STEERABLE_EGRESS_CONFINED: '1' },
      onLogLine: (line) => lines.push(line),
    });
    expect(
      lines.some((l) => l.includes('bwrap') && l.includes('per-host via the egress proxy')),
    ).toBe(true);
  });

  it('wraps the Windows sidecar with win-spawn-helper --passthrough', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true });
    winHelper.exists = true;
    await startAndStop({ sandbox: true });
    expect(execFileImpl).not.toHaveBeenCalled();
    expect(spawnMock).toHaveBeenCalledOnce();
    const [command, args] = spawnMock.mock.calls[0];
    expect(String(command)).toContain('win-spawn-helper');
    expect(args[0]).toBe('--passthrough');
    expect(args).toContain('--writable-root');
    expect(args).toContain('--');
    expect(args.slice(-2)).toEqual(['-m', 'steerable_sidecar']);
  });

  it('refuses start off macOS when linux-wrap fails', async () => {
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    execFileImpl.mockRejectedValue(new Error('no bwrap'));
    const lines: string[] = [];
    await expectRefuse({ sandbox: true, onLogLine: (line) => lines.push(line) }, 'wrap_failed');
    expect(lines.some((l) => l.includes('refusing unsandboxed'))).toBe(true);
  });

  it('refuses start when profile generation fails', async () => {
    execFileImpl.mockRejectedValue(new Error('no sandbox module (old sidecar)'));
    const lines: string[] = [];
    await expectRefuse({ sandbox: true, onLogLine: (line) => lines.push(line) }, 'profile_failed');
    expect(lines.some((l) => l.includes('profile generation failed'))).toBe(true);
  });

  it('refuses start when the generated profile is not a Seatbelt policy', async () => {
    execFileImpl.mockResolvedValue({ stdout: 'not a policy', stderr: '' });
    await expectRefuse({ sandbox: true }, 'profile_failed');
  });

  it('refuses start when Seatbelt is missing on macOS', async () => {
    seatbelt.exists = false;
    await expectRefuse({ sandbox: true }, 'seatbelt_missing');
  });

  it('refuses start when win-spawn-helper is missing', async () => {
    Object.defineProperty(process, 'platform', { value: 'win32', configurable: true });
    winHelper.exists = false;
    await expectRefuse({ sandbox: true }, 'helper_missing');
  });

  it('refuses start on an OS with no process sandbox', async () => {
    Object.defineProperty(process, 'platform', { value: 'freebsd', configurable: true });
    await expectRefuse({ sandbox: true }, 'platform_unsupported');
  });
});

describe('SidecarSupervisor sandbox posture (W4-3 disclosure contract)', () => {
  const savedEnv = { ...process.env };
  const realPlatform = Object.getOwnPropertyDescriptor(process, 'platform')!;

  beforeEach(() => {
    spawnMock.mockReset();
    execFileImpl.mockReset();
    spawnMock.mockImplementation(() => fakeChild());
    execFileImpl.mockResolvedValue({ stdout: PROFILE, stderr: '' });
    delete process.env.STEERABLE_SIDECAR_SANDBOX;
    delete process.env.STEERABLE_RUST_SIDECAR;
    delete process.env.STEERABLE_RUST_SIDECAR_BIN;
    SidecarSupervisor.lastSpawnRefusal = null;
    Object.defineProperty(process, 'platform', { value: 'darwin', configurable: true });
    seatbelt.exists = true;
    winHelper.exists = false;
    rustBin.exists = false;
  });

  afterEach(() => {
    process.env = { ...savedEnv };
    Object.defineProperty(process, 'platform', realPlatform);
  });

  async function startAndStop(options: Parameters<typeof SidecarSupervisor.start>[0]) {
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: '/fake/python3',
      healthIntervalMs: 0,
      ...options,
    });
    await supervisor.shutdown();
    return supervisor;
  }

  // The renderer reads getSandboxPosture() via /api/v2/sidecar/sandbox-posture;
  // a refused start still records lastSpawnRefusal so the settings page can
  // say "无法收容、已拒绝启动" instead of "未沙箱仍在跑".
  it('records wrap_failed on lastSpawnRefusal when linux-wrap fails', async () => {
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    execFileImpl.mockRejectedValue(new Error('no bwrap'));
    await expectRefuse({ sandbox: true }, 'wrap_failed');
  });

  it('records a readable posture on a successful Linux wrap', async () => {
    Object.defineProperty(process, 'platform', { value: 'linux', configurable: true });
    execFileImpl.mockResolvedValue({ stdout: LINUX_WRAP, stderr: '' });
    const supervisor = await startAndStop({ sandbox: true });
    expect(supervisor.getSandboxPosture()).toEqual({
      backend: 'bwrap',
      enforcement: 'partial',
      reason: 'active',
    });
  });

  it('records seatbelt_missing when /usr/bin/sandbox-exec is absent', async () => {
    seatbelt.exists = false;
    await expectRefuse({ sandbox: true }, 'seatbelt_missing');
  });

  it('records profile_failed when profile generation throws', async () => {
    execFileImpl.mockRejectedValue(new Error('no sandbox module (old sidecar)'));
    await expectRefuse({ sandbox: true }, 'profile_failed');
  });

  it('records the explicit opt-out distinctly from involuntary degradation', async () => {
    const byOption = await startAndStop({ sandbox: false });
    expect(byOption.getSandboxPosture()).toEqual({
      backend: 'none',
      enforcement: 'none',
      reason: 'disabled_by_option',
    });

    process.env.STEERABLE_SIDECAR_SANDBOX = '0';
    const byEnv = await startAndStop({});
    expect(byEnv.getSandboxPosture()).toEqual({
      backend: 'none',
      enforcement: 'none',
      reason: 'disabled_by_env',
    });
  });

  it('records an honest partial (never full) on the Seatbelt path', async () => {
    const supervisor = await startAndStop({ sandbox: true });
    expect(supervisor.getSandboxPosture()).toEqual({
      backend: 'seatbelt',
      enforcement: 'partial',
      reason: 'active',
    });
  });
});
