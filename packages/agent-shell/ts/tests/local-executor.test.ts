/**
 * LocalExecutor.executeShell 行为面 + 命令安全 + shell/平台解析。
 *
 * 覆盖现有测试（local-partial-view / local-edit / local-run-code /
 * tool-contract）之外的部分：
 * - executeShell：成功 / 非 0 退出码 / stderr 分流 / env 合并 / cwd /
 *   显式 shell / spawn 错误 / 超时（普通命令终止 vs GUI stillRunning）/
 *   输出截断 / timeout 秒换算 / 默认超时配置；
 * - 危险命令：内置 unix 模式、updateSafetyConfig 的禁用 / 自定义 /
 *   非法正则容错、executeShell 集成拦截（命令不产生副作用）；
 * - isGuiLaunchCommand / rewriteExeCommandIfNeeded / getWineCommand；
 * - getPlatformInfo / init 幂等 / hashContent 性质。
 *
 * POSIX 短命令（printf/sleep/exit）与 unix 危险模式在 Windows 默认
 * PowerShell 下不适用，相关用例 skipIf(win32)；wine 改写在 win32 是
 * 原样返回，用例内按平台分支断言。WSL 探测 / computeDefaultShell 的
 * win32 分支属平台专属，本文件只覆盖 macOS/Linux 可达分支。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import { access, mkdtemp, realpath, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {
  GUI_LAUNCH_WAIT_MS,
  LocalExecutor,
  getConfiguredExecTimeoutMs,
  getDefaultExecTimeoutMs,
  getWineCommand,
  hashContent,
  isGuiLaunchCommand,
  rewriteExeCommandIfNeeded,
  setDefaultExecTimeoutMs,
} from '../src/local-executor.js';

const isWin = process.platform === 'win32';
const isMac = process.platform === 'darwin';
const isLinux = process.platform === 'linux';
// CI Linux 容器可能没装 zsh（spawn ENOENT），显式 zsh 用例按可用性跳过。
const hasZsh = !isWin && spawnSync('zsh', ['--version'], { stdio: 'ignore' }).status === 0;

describe('executeShell · 基础分支', () => {
  it('空命令 / 纯空白命令被拒绝（command is required）', async () => {
    const executor = new LocalExecutor();
    for (const command of ['', '   ']) {
      const res = await executor.executeShell({ command });
      expect(res.success).toBe(false);
      expect(res.error).toBe('command is required');
    }
  });

  it.skipIf(isWin)('成功命令：stdout / exitCode / shell / platform 字段齐全', async () => {
    const res = await new LocalExecutor().executeShell({ command: 'printf hello' });
    expect(res.success).toBe(true);
    expect(res.stdout).toBe('hello');
    expect(res.exitCode).toBe(0);
    expect(res.shell).toBe(isMac ? 'zsh' : 'bash');
    expect(res.platform).toBe(process.platform);
    expect(res.truncated).toBe(false);
    expect(res.timedOut).toBeUndefined();
    expect(res.stillRunning).toBeUndefined();
  });

  it.skipIf(isWin)('非 0 退出码：success=false 且 exitCode 原样透出', async () => {
    const res = await new LocalExecutor().executeShell({ command: 'exit 3' });
    expect(res.success).toBe(false);
    expect(res.exitCode).toBe(3);
  });

  it.skipIf(isWin)('stdout 与 stderr 分流捕获，stderr 不影响退出码判定', async () => {
    const res = await new LocalExecutor().executeShell({
      command: "printf 'out'; printf 'err' >&2",
    });
    expect(res.success).toBe(true);
    expect(res.stdout).toBe('out');
    expect(res.stderr).toBe('err');
  });

  it.skipIf(isWin)('env 合并：request.env 注入到子进程环境', async () => {
    const res = await new LocalExecutor().executeShell({
      command: 'printf %s "$AGENT_SHELL_EXEC_TEST"',
      env: { AGENT_SHELL_EXEC_TEST: 'env-ok-123' },
    });
    expect(res.success).toBe(true);
    expect(res.stdout).toBe('env-ok-123');
  });

  it.skipIf(isWin)('cwd：命令在指定目录下执行（pwd 输出物理路径）', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'exec-cwd-'));
    try {
      // macOS 上 os.tmpdir() 是 /var/... 软链，pwd 输出解析后的物理路径。
      const real = await realpath(dir);
      const res = await new LocalExecutor().executeShell({ command: 'pwd', cwd: dir });
      expect(res.success).toBe(true);
      expect(res.stdout?.trim()).toBe(real);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it.skipIf(isWin)('显式 shell=bash：走 /bin/bash 且结果标记 shell=bash', async () => {
    const res = await new LocalExecutor().executeShell({
      command: 'printf %s "$BASH_VERSION"',
      shell: 'bash',
    });
    expect(res.success).toBe(true);
    expect(res.shell).toBe('bash');
    expect(res.stdout?.trim()).not.toBe('');
  });

  it.skipIf(!hasZsh)('显式 shell=zsh：走 zsh 且结果标记 shell=zsh', async () => {
    const res = await new LocalExecutor().executeShell({
      command: 'printf %s "$ZSH_VERSION"',
      shell: 'zsh',
    });
    expect(res.success).toBe(true);
    expect(res.shell).toBe('zsh');
    expect(res.stdout?.trim()).not.toBe('');
  });

  it.skipIf(isWin)('shell=wsl 在无 WSL 的平台：spawn 错误映射为 success=false', async () => {
    // macOS/Linux 没有 wsl.exe，spawn 触发 ENOENT → child 'error' 事件分支。
    const res = await new LocalExecutor().executeShell({ command: 'echo hi', shell: 'wsl' });
    expect(res.success).toBe(false);
    expect(res.error).toBeTruthy();
    expect(res.shell).toBe('wsl');
  });

  it('cwd 指向不存在目录：spawn 失败返回错误结果而不是抛异常', async () => {
    const res = await new LocalExecutor().executeShell({
      command: 'echo hi',
      cwd: path.join(os.tmpdir(), `agent-shell-no-such-dir-${process.pid}`),
    });
    expect(res.success).toBe(false);
    expect(res.error).toBeTruthy();
  });
});

describe('executeShell · 超时与 GUI', () => {
  it('GUI_LAUNCH_WAIT_MS 导出值为 15s', () => {
    expect(GUI_LAUNCH_WAIT_MS).toBe(15_000);
  });

  it.skipIf(isWin)('非 GUI 命令超时：进程被终止，timedOut=true 且提示不要盲重跑', async () => {
    const start = Date.now();
    const res = await new LocalExecutor().executeShell({ command: 'sleep 30', timeout: 1 });
    const elapsed = Date.now() - start;
    expect(res.success).toBe(false);
    expect(res.timedOut).toBe(true);
    expect(res.stillRunning).toBeUndefined();
    expect(res.error).toContain('timed out');
    expect(res.error).toContain('DO NOT blindly re-run');
    // timeout=1 按秒换算成 1000ms（<1000 的值视为秒）。
    expect(elapsed).toBeGreaterThanOrEqual(900);
    expect(elapsed).toBeLessThan(10_000);
  });

  it.skipIf(isWin)('timeout 秒换算：timeout=2 约 2000ms 后才终止', async () => {
    const start = Date.now();
    const res = await new LocalExecutor().executeShell({ command: 'sleep 30', timeout: 2 });
    const elapsed = Date.now() - start;
    expect(res.timedOut).toBe(true);
    expect(elapsed).toBeGreaterThanOrEqual(1_800);
    expect(elapsed).toBeLessThan(10_000);
  });

  it.skipIf(isWin)('GUI 启动命令超时：stillRunning 成功语义，进程不被杀', async () => {
    // 命令行带独立 "gui" token → isGuiLaunchCommand 命中；sleep 2 保证
    // 1s 超时点进程仍存活。超时后按"已启动、仍在运行"成功返回，进程留着
    // （约 1s 后自行退出，不影响后续用例）。
    const res = await new LocalExecutor().executeShell({
      command: 'sleep 2; echo gui',
      timeout: 1,
    });
    expect(res.success).toBe(true);
    expect(res.timedOut).toBe(true);
    expect(res.stillRunning).toBe(true);
    expect(res.stdout).toContain('GUI 程序已启动');
    expect(res.stdout).toContain('仍在运行');
  });
});

describe('executeShell · 默认超时配置', () => {
  afterEach(() => {
    // 模块级全局状态，用完恢复内置默认。
    setDefaultExecTimeoutMs(null);
  });

  it('setter/getter：合法值生效，<1000 与 null/undefined 回落内置默认', () => {
    expect(getDefaultExecTimeoutMs()).toBe(30_000);
    expect(getConfiguredExecTimeoutMs()).toBeNull();

    setDefaultExecTimeoutMs(5_000);
    expect(getDefaultExecTimeoutMs()).toBe(5_000);
    expect(getConfiguredExecTimeoutMs()).toBe(5_000);

    setDefaultExecTimeoutMs(500); // <1000 视为非法，回落
    expect(getDefaultExecTimeoutMs()).toBe(30_000);
    expect(getConfiguredExecTimeoutMs()).toBeNull();

    setDefaultExecTimeoutMs(60_000);
    setDefaultExecTimeoutMs(undefined);
    expect(getDefaultExecTimeoutMs()).toBe(30_000);
    expect(getConfiguredExecTimeoutMs()).toBeNull();
  });

  it.skipIf(isWin)('未显式给 timeout 时使用配置的默认超时', async () => {
    setDefaultExecTimeoutMs(1_000);
    const res = await new LocalExecutor().executeShell({ command: 'sleep 30' });
    expect(res.success).toBe(false);
    expect(res.timedOut).toBe(true);
  });
});

describe('executeShell · 输出截断', () => {
  it.skipIf(isWin)('stdout 超过 maxOutputBytes：截断并标记 truncated', async () => {
    const executor = new LocalExecutor(16);
    const res = await executor.executeShell({ command: "printf 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'" });
    expect(res.success).toBe(true);
    expect(res.truncated).toBe(true);
    expect(res.stdout).toBe('A'.repeat(16));
  });

  it.skipIf(isWin)('stderr 超过 maxOutputBytes：同样截断', async () => {
    const executor = new LocalExecutor(16);
    const res = await executor.executeShell({
      command: "printf 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB' >&2",
    });
    expect(res.success).toBe(true);
    expect(res.truncated).toBe(true);
    expect(res.stderr).toBe('B'.repeat(16));
  });

  it.skipIf(isWin)('输出未超限：truncated=false', async () => {
    const executor = new LocalExecutor(1024);
    const res = await executor.executeShell({ command: "printf 'short'" });
    expect(res.success).toBe(true);
    expect(res.truncated).toBe(false);
    expect(res.stdout).toBe('short');
  });
});

describe('危险命令拦截 · 内置 unix 模式', () => {
  it.skipIf(isWin)('rm -rf / / sudo / mkfs / dd if= / chmod -R 777 / / fork bomb 均被识别', () => {
    const executor = new LocalExecutor();
    const blocked = [
      'rm -rf /',
      'rm -rf / --no-preserve-root',
      'sudo echo hi',
      'mkfs.ext4 /dev/sda1',
      'dd if=/dev/zero of=/dev/sda',
      'chmod -R 777 /',
      ':(){ :|:& };:',
    ];
    for (const cmd of blocked) {
      expect(executor.detectDangerousCommand(cmd), cmd).not.toBeNull();
    }
  });

  it.skipIf(isWin)('形近但安全的命令不误伤；windows 模式在 unix 不加载', () => {
    const executor = new LocalExecutor();
    const safe = [
      'rm -rf /tmp/scratch', // "/" 后非空白/结尾，不算抹根
      'rm -rf ./build',
      'echo sudo', // sudo 后无空白
      'sudoers is a file',
      'chmod 777 ./run.sh', // 无 -R 且目标非根
      'dd status=progress', // 无 if=
      'format c:', // windows 模式，unix 平台不加载
      'del /f /s /q c:\\temp',
    ];
    for (const cmd of safe) {
      expect(executor.detectDangerousCommand(cmd), cmd).toBeNull();
    }
  });

  it.skipIf(isWin)('updateSafetyConfig：disabledPatternIds 禁用内置模式，且配置是实例级的', () => {
    const executor = new LocalExecutor();
    executor.updateSafetyConfig({ disabledPatternIds: ['sudo'], customPatterns: [] });
    expect(executor.detectDangerousCommand('sudo echo hi')).toBeNull();
    expect(executor.detectDangerousCommand('rm -rf /')).not.toBeNull();
    // 新实例不受影响的默认全集。
    expect(new LocalExecutor().detectDangerousCommand('sudo echo hi')).not.toBeNull();
  });

  it('updateSafetyConfig：启用的自定义模式生效，disabled 不生效，非法正则跳过不炸', () => {
    const executor = new LocalExecutor();
    executor.updateSafetyConfig({
      disabledPatternIds: [],
      customPatterns: [
        { id: 'c1', label: '危险区', pattern: 'dangerzone', category: 'custom', enabled: true },
        { id: 'c2', label: '未启用', pattern: 'offlimits', category: 'custom', enabled: false },
        { id: 'c3', label: '坏正则', pattern: '[', category: 'custom', enabled: true },
      ],
    });
    expect(executor.detectDangerousCommand('echo dangerzone')).not.toBeNull();
    expect(executor.detectDangerousCommand('echo offlimits')).toBeNull();
    // 非法正则被跳过（log.warn），内置模式仍然工作。
    if (!isWin) {
      expect(executor.detectDangerousCommand('sudo ls')).not.toBeNull();
    }
  });

  it.skipIf(isWin)('executeShell 集成：危险命令在 spawn 前被拦截，不产生副作用', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'danger-'));
    const marker = path.join(dir, 'created.txt');
    try {
      const res = await new LocalExecutor().executeShell({ command: `sudo touch ${marker}` });
      expect(res.success).toBe(false);
      expect(res.error).toContain('Blocked dangerous command');
      // 拦截发生在 spawn 之前：命令若真执行会创建 marker 文件。
      await expect(access(marker)).rejects.toThrow();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

describe('isGuiLaunchCommand', () => {
  it('独立 gui token（子命令 / --gui / start_gui.bat / 括号边界）识别为 GUI 启动', () => {
    const gui = ['myapp gui replay', 'myapp --gui', 'start_gui.bat', 'foo GUI bar', 'tool(gui)mode'];
    for (const cmd of gui) {
      expect(isGuiLaunchCommand(cmd), cmd).toBe(true);
    }
  });

  it('guide / guid / mygui 等词内片段不误伤；空命令安全', () => {
    const notGui = ['guide me', 'echo guid', 'mygui', 'guile', ''];
    for (const cmd of notGui) {
      expect(isGuiLaunchCommand(cmd), cmd).toBe(false);
    }
  });
});

describe('rewriteExeCommandIfNeeded · wine 改写', () => {
  // win32 上函数原样返回（无需 wine）；POSIX 上 runner 是探测到的
  // wine64/wine，未安装时回落字面量 'wine'。
  const runner = isWin ? null : getWineCommand() || 'wine';
  const expectRewrite = (input: string, posixExpected: string) => {
    expect(rewriteExeCommandIfNeeded(input)).toBe(isWin ? input : posixExpected);
  };

  it('命令位置的 .exe 加 wine 前缀', () => {
    expectRewrite('myapp.exe --flag', `${runner} myapp.exe --flag`);
  });

  it('引号包裹的 exe 路径保留引号', () => {
    expectRewrite('"C:\\tools\\my app.exe" run', `${runner} "C:\\tools\\my app.exe" run`);
    expectRewrite("'C:\\tools\\app.exe'", `${runner} 'C:\\tools\\app.exe'`);
  });

  it('&& 与管道之后的 exe 同样改写（命令位置）', () => {
    expectRewrite('echo a && run.exe', `echo a && ${runner} run.exe`);
    expectRewrite('cat log | parser.exe', `cat log | ${runner} parser.exe`);
  });

  it('多个 exe 各自加前缀；.EXE 大小写不敏感', () => {
    expectRewrite('a.exe | b.exe', `${runner} a.exe | ${runner} b.exe`);
    expectRewrite('APP.EXE', `${runner} APP.EXE`);
  });

  it('参数位置的 .exe 不改写；已有 wine 前缀不重复加', () => {
    expectRewrite('echo foo.exe', 'echo foo.exe');
    expectRewrite('wine app.exe', 'wine app.exe');
  });

  it('无 exe 的命令与空串原样返回', () => {
    expectRewrite('echo hello', 'echo hello');
    expect(rewriteExeCommandIfNeeded('')).toBe('');
  });
});

describe('getWineCommand', () => {
  it('结果缓存：两次调用返回同一值；win32 恒为 null', () => {
    const first = getWineCommand();
    const second = getWineCommand();
    expect(first).toBe(second);
    if (isWin) {
      expect(first).toBeNull();
    } else {
      expect([null, 'wine', 'wine64']).toContain(first);
    }
  });
});

describe('getPlatformInfo / init', () => {
  it('平台 / 默认 shell / 架构信息齐全；WSL 仅 win32 探测', async () => {
    const executor = new LocalExecutor();
    await executor.init();
    const info = executor.getPlatformInfo();
    expect(info.platform).toBe(process.platform);
    expect(info.osVersion).toBeTruthy();
    expect(info.osArch).toBe(os.arch());
    if (isMac) expect(info.shell).toBe('zsh');
    else if (isLinux) expect(info.shell).toBe('bash');
    else expect(info.shell).toBe('powershell');
    if (!isWin) expect(info.wslAvailable).toBe(false);
  });

  it('init 幂等：重复调用不报错', async () => {
    const executor = new LocalExecutor();
    await executor.init();
    await executor.init();
  });
});

describe('hashContent', () => {
  it('64 位小写 hex，内容敏感（契约向量一致性见 tool-contract.test.ts）', () => {
    const h = hashContent('agent-shell');
    expect(h).toMatch(/^[0-9a-f]{64}$/);
    expect(hashContent('agent-shell')).toBe(h);
    expect(hashContent('agent-shell-2')).not.toBe(h);
    // 空串 SHA-256 是公开常量，顺手钉住编码（utf-8）语义。
    expect(hashContent('')).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  });
});
