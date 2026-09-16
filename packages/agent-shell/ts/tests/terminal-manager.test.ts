/**
 * Integration tests for `TerminalManager.exec()` against a real PTY.
 *
 * These spawn actual `cmd.exe` / `powershell.exe` processes via `node-pty`,
 * so they're Windows-only (the bugs being regression-tested — `%ERRORLEVEL%`
 * parse-time expansion, the `node -e` dependency in the PowerShell sentinel,
 * and Windows-flavoured exit codes like `9009` — are Windows-specific).
 */
import { describe, expect, it } from 'vitest';
import { TerminalManager } from '../src/terminal-manager.js';

const isWindows = process.platform === 'win32';
const win = isWindows ? describe : describe.skip;

win('TerminalManager.exec() on cmd.exe', () => {
  it('reports the freshly-set exit code for each command instead of a stale one', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'cmd.exe' });
    try {
      const ok = await manager.exec(session.id, 'echo hi', 10_000);
      expect(ok.success).toBe(true);
      expect(ok.exitCode).toBe(0);

      // Windows' well-known "command not recognized" exit code. Under the old
      // `%ERRORLEVEL%` (parse-time expansion) bug this test's own command was
      // never actually reflected — the value observed was always the
      // *previous* command's exit code.
      const bad = await manager.exec(session.id, 'this_is_not_a_real_command_xyz', 10_000);
      expect(bad.success).toBe(false);
      expect(bad.exitCode).toBe(9009);

      // Regression guard: a *subsequent* successful command must not still
      // report the previous failure's code.
      const after = await manager.exec(session.id, 'echo ok', 10_000);
      expect(after.success).toBe(true);
      expect(after.exitCode).toBe(0);
    } finally {
      manager.killAll();
    }
  }, 20_000);
});

win('TerminalManager.exec() on powershell.exe', () => {
  it('emits the sentinel natively (no external `node` dependency) and reports $? as 0/1', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'powershell.exe' });
    try {
      const ok = await manager.exec(session.id, 'Write-Host hi', 10_000);
      expect(ok.success).toBe(true);
      expect(ok.exitCode).toBe(0);
      expect(ok.stdout).toContain('hi');

      const bad = await manager.exec(session.id, 'Get-Item nonexistent-file-xyz-zzz', 10_000);
      expect(bad.success).toBe(false);
      expect(bad.exitCode).toBe(1);
    } finally {
      manager.killAll();
    }
  }, 20_000);
});

win('TerminalManager.exec() truncation flag', () => {
  it('flags truncated output instead of always reporting false', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'cmd.exe' });
    try {
      const small = await manager.exec(session.id, 'echo hi', 10_000);
      expect(small.truncated).toBe(false);

      // Ask PowerShell (available on any Windows box) to print well over the
      // 256 KiB exec capture cap in one command.
      const big = await manager.exec(
        session.id,
        `powershell -NoProfile -Command "('x' * 300000)"`,
        15_000
      );
      expect(big.truncated).toBe(true);
    } finally {
      manager.killAll();
    }
  }, 30_000);
});

win('TerminalManager.exec() PTY exit while a command is pending', () => {
  it('rejects promptly instead of hanging until the exec timeout', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'cmd.exe' });
    const timeoutMs = 20_000;
    const start = Date.now();
    // `exit` terminates cmd.exe immediately — before the `& node -e ...`
    // sentinel half of the wrapped line ever runs — simulating a shell that
    // dies mid-command.
    await expect(manager.exec(session.id, 'exit', timeoutMs)).rejects.toThrow(/exited/);
    const elapsed = Date.now() - start;
    // Should resolve almost immediately via the onExit handler, nowhere near
    // the full exec timeout.
    expect(elapsed).toBeLessThan(timeoutMs / 2);
  }, 25_000);
});

const posix = isWindows ? describe.skip : describe;

// bash, not zsh: CI runners (ubuntu-latest) have no zsh installed. The
// SIGINT semantics under test hold for both — zsh aborts the wrapper's
// command list (recovery probe resolves the exec), bash continues it
// (normal sentinel resolves it with $? = 130).
posix('TerminalManager.exec() killOnTimeout', () => {
  it('SIGINTs a stuck command and the shared terminal stays usable', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'bash' });
    try {
      // Warm up: shell init files can take seconds; an exec only returns
      // once the shell processed input, so this doubles as a readiness gate.
      await manager.exec(session.id, 'true', 20_000);

      // Without killOnTimeout this command would keep running in the PTY and
      // every later exec would jam behind it and time out in cascade.
      const stuck = await manager.exec(session.id, 'sleep 60', 1_000, true);
      expect(stuck.success).toBe(false);
      expect(stuck.exitCode).toBe(130);
      expect(stuck.stderr).toContain('interrupted');

      // The terminal must answer immediately afterwards — no cascade.
      const after = await manager.exec(session.id, 'echo terminal-back', 10_000);
      expect(after.success).toBe(true);
      expect(after.stdout).toContain('terminal-back');
    } finally {
      manager.killAll();
    }
  }, 45_000);

  it('still rejects without SIGINT when killOnTimeout is off', async () => {
    const manager = new TerminalManager();
    const session = manager.spawn({ shell: 'bash' });
    try {
      await expect(manager.exec(session.id, 'sleep 60', 1_000)).rejects.toThrow(
        /exec timeout after 1000ms/
      );
      // Clean up the still-running sleep so it can't jam later tests.
      manager.killAll();
    } finally {
      manager.killAll();
    }
  }, 15_000);
});
