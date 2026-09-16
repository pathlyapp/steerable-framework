import { afterEach, describe, expect, it } from 'vitest';
import {
  GUI_LAUNCH_WAIT_MS,
  LocalExecutor,
  getConfiguredExecTimeoutMs,
  getDefaultExecTimeoutMs,
  isGuiLaunchCommand,
  setDefaultExecTimeoutMs,
} from '../../src/local-executor.js';

describe('isGuiLaunchCommand', () => {
  it('matches standalone "gui" tokens in CIFLog-style launch commands', () => {
    expect(isGuiLaunchCommand('CIFLogNet.exe gui replay card.xml')).toBe(true);
    expect(isGuiLaunchCommand('"C:\\Program Files\\CIFLog\\app.exe" gui')).toBe(true);
    expect(isGuiLaunchCommand('myapp --gui --port 7999')).toBe(true);
    expect(isGuiLaunchCommand('start_gui.bat')).toBe(true);
    expect(isGuiLaunchCommand('python run.py --mode=gui')).toBe(true);
    expect(isGuiLaunchCommand('GUI replay')).toBe(true); // 大小写不敏感
  });

  it('does not match "gui" embedded inside other words', () => {
    expect(isGuiLaunchCommand('cat user-guide.md')).toBe(false);
    expect(isGuiLaunchCommand('echo guid-1234')).toBe(false);
    expect(isGuiLaunchCommand('run myguitool.exe')).toBe(false);
    expect(isGuiLaunchCommand('ambiguity check')).toBe(false);
    expect(isGuiLaunchCommand('')).toBe(false);
  });

  it('exposes a sane launch grace period', () => {
    expect(GUI_LAUNCH_WAIT_MS).toBeGreaterThanOrEqual(5_000);
  });
});

describe('executeShell gui timeout behavior', () => {
  it('reports a still-running gui command as success instead of killing it', async () => {
    const executor = new LocalExecutor();
    // `node -e ...` 在 powershell/zsh/bash 下语法一致；结尾的 `gui` 只是被
    // node 忽略的多余参数，用来触发 isGuiLaunchCommand。进程睡 2.5s，超时 1.2s。
    const result = await executor.executeShell({
      command: 'node -e "setTimeout(function(){}, 2500)" gui',
      timeout: 1200,
    });
    expect(result.success).toBe(true);
    expect(result.timedOut).toBe(true);
    expect(result.stillRunning).toBe(true);
    expect(result.stdout).toContain('GUI 程序已启动');
  }, 10_000);

  it('still fails (and kills) a timed-out non-gui command', async () => {
    const executor = new LocalExecutor();
    const result = await executor.executeShell({
      command: 'node -e "setTimeout(function(){}, 2500)"',
      timeout: 1200,
    });
    expect(result.success).toBe(false);
    expect(result.timedOut).toBe(true);
    expect(result.stillRunning).toBeUndefined();
  }, 10_000);
});

describe('configurable default exec timeout', () => {
  afterEach(() => {
    setDefaultExecTimeoutMs(null);
  });

  it('defaults to the builtin 30s when unset', () => {
    expect(getConfiguredExecTimeoutMs()).toBeNull();
    expect(getDefaultExecTimeoutMs()).toBe(30_000);
  });

  it('applies a user-configured default', () => {
    setDefaultExecTimeoutMs(120_000);
    expect(getConfiguredExecTimeoutMs()).toBe(120_000);
    expect(getDefaultExecTimeoutMs()).toBe(120_000);
  });

  it('rejects nonsense values (sub-second or null) and falls back to builtin', () => {
    setDefaultExecTimeoutMs(500);
    expect(getConfiguredExecTimeoutMs()).toBeNull();
    setDefaultExecTimeoutMs(null);
    expect(getDefaultExecTimeoutMs()).toBe(30_000);
  });
});
