/**
 * 可见 PTY 执行路由（host/visible-terminal-exec.ts）行为测试。
 *
 * 钉住的契约：
 *  - 多行/heredoc 命令回退 headless（sentinel 抓不住）；
 *  - 危险命令与 headless 路径同策略拦截；
 *  - 超时语义：GUI 启动命令到点按「已启动、仍在运行」（success:true，
 *    绝不重跑）；普通命令超时返回 timedOut 错误且**不**回退 headless
 *    （回退会把同一条命令原样再跑一遍——「Qt 图件被启动多次」的根源）；
 *  - 非超时执行错误才回退 headless（返回 null）；
 *  - 项目模式显式 cd 到 req.cwd（共享 session 不会自己换目录）；
 *  - shell 枚举映射让 agent 看到真实方言。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  detectDangerousCommand: vi.fn(() => null as string | null),
  ensurePrimary: vi.fn(),
  terminalExec: vi.fn(),
  getConfiguredExecTimeoutMs: vi.fn(() => null as number | null),
}));

vi.mock('../src/local-executor.js', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../src/local-executor.js')>();
  return {
    ...mod,
    getConfiguredExecTimeoutMs: mocks.getConfiguredExecTimeoutMs,
  };
});

import {
  createVisibleTerminalExec,
  mapSessionShell,
} from '../src/host/visible-terminal-exec.js';
import type { LocalExecRequest } from '../src/local-executor.js';

function makeExec() {
  const deps = {
    localExecutor: { detectDangerousCommand: mocks.detectDangerousCommand },
    terminalManager: { ensurePrimary: mocks.ensurePrimary, exec: mocks.terminalExec },
  };
  return createVisibleTerminalExec(deps as never);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.detectDangerousCommand.mockReturnValue(null);
  mocks.ensurePrimary.mockReturnValue({ id: 'main', shell: 'zsh', cwd: '/home' });
  mocks.terminalExec.mockResolvedValue({
    success: true,
    stdout: 'out',
    stderr: '',
    exitCode: 0,
    truncated: false,
    durationMs: 5,
  });
  mocks.getConfiguredExecTimeoutMs.mockReturnValue(null);
});

describe('mapSessionShell', () => {
  it('powershell/pwsh/cmd/wsl/bash/zsh 各归其类', () => {
    expect(mapSessionShell('powershell.exe')).toBe('powershell');
    expect(mapSessionShell('pwsh')).toBe('powershell');
    expect(mapSessionShell('cmd.exe')).toBe('cmd');
    expect(mapSessionShell('wsl.exe')).toBe('wsl');
    expect(mapSessionShell('/bin/bash')).toBe('bash');
    expect(mapSessionShell('/bin/zsh')).toBe('zsh');
  });

  it('未知 shell 按平台回退（macOS/Linux → zsh）', () => {
    expect(mapSessionShell('fish')).toBe(process.platform === 'win32' ? 'powershell' : 'zsh');
  });
});

describe('maybeExecInTerminal · 路由决策', () => {
  it('多行命令回退 headless（返回 null，不碰终端）', async () => {
    const exec = makeExec();
    expect(await exec({ command: 'ls\ncat x' })).toBeNull();
    expect(mocks.ensurePrimary).not.toHaveBeenCalled();
  });

  it('危险命令被拦截（与 headless 同策略），不进入终端', async () => {
    mocks.detectDangerousCommand.mockReturnValue('rm -rf /');
    const exec = makeExec();
    const result = await exec({ command: 'rm -rf /' });
    expect(result).toMatchObject({ success: false });
    expect(result?.error).toContain('Blocked dangerous command');
    expect(mocks.ensurePrimary).not.toHaveBeenCalled();
  });

  it('正常命令：执行 + 结果带真实 shell 枚举', async () => {
    const exec = makeExec();
    const result = await exec({ command: 'ls' });
    expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'ls', undefined, true);
    expect(result).toMatchObject({ success: true, stdout: 'out', shell: 'zsh' });
  });

  it('项目模式：命令前显式 cd（引号转义）', async () => {
    const exec = makeExec();
    await exec({ command: 'ls', cwd: '/proj/dir"q' });
    expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'cd "/proj/dir\\"q" && ls', undefined, true);
  });

  it('超时归一化：小于 1000 的数按秒理解（×1000）', async () => {
    const exec = makeExec();
    await exec({ command: 'ls', timeout: 30 });
    expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'ls', 30_000, true);
  });

  it('未显式给 timeout 时用配置的全局默认', async () => {
    mocks.getConfiguredExecTimeoutMs.mockReturnValue(60_000);
    const exec = makeExec();
    await exec({ command: 'ls' });
    expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'ls', 60_000, true);
  });
});

describe('maybeExecInTerminal · 超时语义', () => {
  it('GUI 启动命令：到点未退出 → success:true + stillRunning，进程不被杀', async () => {
    mocks.terminalExec.mockRejectedValue(new Error('exec timeout after 3000ms'));
    const exec = makeExec();
    // isGuiLaunchCommand 真实实现：独立 "gui" token（--gui）即识别为 GUI 启动
    const result = await exec({ command: 'myapp --gui' });
    expect(result).toMatchObject({ success: true, timedOut: true, stillRunning: true });
    expect(result?.stdout).toContain('GUI 程序已启动');
    // killOnTimeout=false：GUI 进程绝不能被 SIGINT
    expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'myapp --gui', expect.any(Number), false);
  });

  it('GUI 命令未显式给 timeout 时用 GUI_LAUNCH_WAIT_MS 短等待', async () => {
    const exec = makeExec();
    await exec({ command: 'myapp --gui' });
    const timeoutArg = mocks.terminalExec.mock.calls[0][2] as number;
    expect(timeoutArg).toBeGreaterThan(0);
    expect(timeoutArg).toBeLessThanOrEqual(30_000); // GUI_LAUNCH_WAIT_MS 量级
  });

  it('普通命令超时：success:false + timedOut，绝不回退 headless 重跑', async () => {
    mocks.terminalExec.mockRejectedValue(new Error('exec timeout after 30000ms'));
    const exec = makeExec();
    const result = await exec({ command: 'sleep 60', timeout: 30_000 });
    expect(result).not.toBeNull();
    expect(result).toMatchObject({ success: false, timedOut: true });
    expect(result?.error).toContain('DO NOT blindly re-run');
  });

  it('非超时执行错误（PTY 挂了）→ null 回退 headless', async () => {
    mocks.terminalExec.mockRejectedValue(new Error('pty died'));
    const exec = makeExec();
    expect(await exec({ command: 'ls' })).toBeNull();
  });
});
