/**
 * 可见 PTY 执行路由：shell 命令优先打进共享的可见终端，多行/heredoc 等
 * sentinel 抓不住的形态回退 headless。Electron main 与 BS server 共用。
 * 命令进入 PTY 时不通知 renderer 展开面板——终端只由用户手动打开。
 */
import log from 'electron-log';
import {
  GUI_LAUNCH_WAIT_MS,
  getConfiguredExecTimeoutMs,
  isGuiLaunchCommand,
  rewriteExeCommandIfNeeded,
  type LocalExecRequest,
  type LocalExecResult,
  type LocalExecutor,
} from '../local-executor.js';
import type { TerminalManager } from '../terminal-manager.js';

export interface VisibleTerminalExecDeps {
  localExecutor: LocalExecutor;
  terminalManager: TerminalManager;
}

/**
 * Map a raw PTY shell binary name (e.g. `powershell.exe`, `cmd.exe`, `zsh`) to
 * the {@link LocalExecResult} `shell` enum so the agent sees the *real* dialect
 * it just ran under and can pick the right syntax next turn.
 */
export function mapSessionShell(shell: string): LocalExecResult['shell'] {
  const s = (shell || '').toLowerCase();
  if (s.includes('powershell') || s.includes('pwsh')) return 'powershell';
  if (s.includes('cmd')) return 'cmd';
  if (s.includes('wsl')) return 'wsl';
  if (s.includes('bash')) return 'bash';
  if (s.includes('zsh')) return 'zsh';
  return process.platform === 'win32' ? 'powershell' : 'zsh';
}

/**
 * Decide whether to route a shell command through the visible PTY instead of
 * the headless local-executor. Returns null to fall back headless. No renderer
 * needs to be attached — the exec sentinel capture runs entirely in this
 * process.
 */
export function createVisibleTerminalExec(deps: VisibleTerminalExecDeps) {
  const { localExecutor, terminalManager } = deps;

  return async function maybeExecInTerminal(req: LocalExecRequest): Promise<LocalExecResult | null> {
    if (req.command) {
      req.command = rewriteExeCommandIfNeeded(req.command);
    }
    // Multi-line / heredoc commands break the sentinel pattern; fall back to
    // headless local-executor for those.
    if (req.command && req.command.includes('\n')) {
      log.info('[terminal-exec] skip multiline command, fallback headless');
      return null;
    }

    // The visible-terminal path must run the same dangerous-command check as
    // headless `executeShell` — both paths enforce the same safety policy.
    if (req.command) {
      const dangerousReason = localExecutor.detectDangerousCommand(req.command);
      if (dangerousReason) {
        log.warn('[terminal-exec] blocked dangerous command', { pattern: dangerousReason });
        return {
          success: false,
          error: `Blocked dangerous command: ${dangerousReason}`,
          platform: process.platform,
        };
      }
    }

    // GUI/交互式命令（命令行带 "gui" token——场景应用启动图形图件的
    // 命令行约定）：没显式 timeout 时只等一小段，到点按"已启动"处理
    // （见下方 catch）。
    const guiCommand = isGuiLaunchCommand(req.command || '');
    let timeoutMs = req.timeout;
    if (timeoutMs !== undefined) {
      if (timeoutMs > 0 && timeoutMs < 1000) {
        timeoutMs *= 1000;
      }
    } else if (guiCommand) {
      timeoutMs = GUI_LAUNCH_WAIT_MS;
    } else {
      timeoutMs = getConfiguredExecTimeoutMs() ?? undefined;
    }

    const session = terminalManager.ensurePrimary({ cwd: req.cwd });
    const resolvedShell = mapSessionShell(session.shell);
    // 项目模式（ToolRouter 会把项目根塞进 req.cwd）：可见 PTY 是共享交互
    // 会话，已有 session 不会随 ensurePrimary 改目录——显式 cd 过去。
    const commandForPty = req.cwd
      ? `cd "${req.cwd.replace(/"/g, '\\"')}" && ${req.command}`
      : req.command;
    log.info('[terminal-exec] start', {
      sessionId: session.id,
      cwd: req.cwd || session.cwd,
      shell: resolvedShell,
      timeout: timeoutMs,
      commandPreview: (req.command || '').slice(0, 200),
    });
    try {
      // killOnTimeout: a timed-out non-GUI command is SIGINTed so it can't jam
      // the shared visible terminal for every subsequent exec (GUI launches
      // keep the "launched, still running" semantics — never killed).
      const r = await terminalManager.exec(session.id, commandForPty, timeoutMs, !guiCommand);
      log.info('[terminal-exec] done', {
        sessionId: session.id,
        success: r.success,
        exitCode: r.exitCode,
        stdoutBytes: (r.stdout || '').length,
        stderrBytes: (r.stderr || '').length,
        durationMs: r.durationMs,
        truncated: r.truncated,
      });
      return {
        success: r.success,
        stdout: r.stdout,
        stderr: r.stderr,
        exitCode: r.exitCode,
        truncated: r.truncated,
        shell: resolvedShell,
        platform: process.platform,
      };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // 超时绝不能 fallback headless——那会把同一条命令原样再跑一遍
      // （GUI/启动类命令会被重复拉起，正是"Qt 图件被启动多次"的根源）。
      if (message.includes('exec timeout after')) {
        if (guiCommand) {
          log.info('[terminal-exec] gui command still running after wait, treat as launched', {
            commandPreview: (req.command || '').slice(0, 120),
          });
          return {
            success: true,
            timedOut: true,
            stillRunning: true,
            stdout:
              `[GUI 程序已启动，仍在终端中运行（等待 ${timeoutMs}ms 后未退出，进程未被终止）。` +
              `请在打开的界面中继续操作；不要重新执行同一条启动命令。]`,
            stderr: '',
            shell: mapSessionShell(session.shell),
            platform: process.platform,
          };
        }
        log.warn('[terminal-exec] timed out, NOT falling back (would re-run command)', message);
        return {
          success: false,
          timedOut: true,
          error:
            `Command timed out after ${timeoutMs}ms in the visible terminal (the process may still be running). ` +
            `DO NOT blindly re-run the same command — if it launched a GUI/long-running program, ` +
            `first check whether it is already running, or re-run with a larger \`timeout\`.`,
          shell: mapSessionShell(session.shell),
          platform: process.platform,
        };
      }
      log.warn('[terminal-exec] failed, fallback headless', message);
      return null;
    }
  };
}
