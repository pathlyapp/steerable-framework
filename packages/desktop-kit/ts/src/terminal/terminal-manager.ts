import { EventEmitter } from 'node:events';
import os from 'node:os';
import { randomUUID } from 'node:crypto';
import log from 'electron-log';
import * as pty from 'node-pty';

import type {
  TerminalSession,
  TerminalSpawnOptions,
  TerminalExecResult,
} from '../bridge/types.js';

export type TerminalShell = 'zsh' | 'bash' | 'sh' | 'powershell.exe' | 'cmd.exe';

export interface TerminalManagerEvents {
  data: (sessionId: string, chunk: string) => void;
  exit: (sessionId: string, code: number, signal: string | null) => void;
  spawned: (session: TerminalSession) => void;
}

const DEFAULT_COLS = 100;
const DEFAULT_ROWS = 30;
const SENTINEL_PREFIX = '__DP_END_';
const MAX_EXEC_OUTPUT_BYTES = 256 * 1024; // 256 KiB cap per command capture
const DEFAULT_EXEC_TIMEOUT_MS = 60_000;
const REPLAY_BUFFER_BYTES = 64 * 1024; // per-session ring buffer for late subscribers

// Build the sentinel regex against raw PTY bytes. We wrap the agent's command
// in a `printf` that emits an **OSC 9999** private control sequence carrying
// our nonce + exit code. Conformant terminal emulators (xterm.js included)
// silently discard unknown OSC commands, so the user never sees this marker
// rendered in the visible terminal — but our capture parser sees the raw
// bytes before any ANSI processing, so it still finds the exit code.
//
//   ESC ] 9999 ; __DP_END_<nonce>__:<exitCode> BEL
//   \u001b]9999;__DP_END_<n>__:0\u0007
function buildSentinelRegex(nonce: string): RegExp {
  // eslint-disable-next-line no-control-regex
  return new RegExp(`\u001b\\]9999;${SENTINEL_PREFIX}${nonce}__:(-?\\d+)\u0007`);
}

// ANSI escape sequence stripper. Covers CSI (\x1b[...), OSC (\x1b]...\x07 or \x1b\\),
// SS2/SS3 (\x1bN /\x1bO), and bare control sequences. This is the de-facto regex
// used by `strip-ansi` — inlined to avoid pulling another dep into the main process.
// eslint-disable-next-line no-control-regex
const ANSI_RE = /[\u001B\u009B][[\]()#;?]*(?:(?:(?:[a-zA-Z\d]*(?:;[a-zA-Z\d]*)*)?\u0007)|(?:(?:\d{1,4}(?:;\d{0,4})*)?[\dA-PR-TZcf-ntqry=><~]))/g;

function stripAnsi(input: string): string {
  return input.replace(ANSI_RE, '');
}

interface SessionEntry {
  id: string;
  proc: pty.IPty;
  shell: string;
  cwd: string;
  cols: number;
  rows: number;
  /**
   * Buffer used to capture command output for `exec()`. When set, raw PTY data
   * is appended here (in addition to being broadcast as a `data` event so the
   * visible xterm UI keeps rendering). Becomes `null` when no exec is pending.
   */
  capture: { buffer: string; nonce: string; resolve: (out: string) => void } | null;
  /**
   * Ring buffer of recent PTY output. Replayed when a renderer subscribes after
   * the session was already producing output (e.g. the terminal window opens
   * mid-stream because the agent kicked off a command before the user opened it).
   */
  replay: string;
}

/**
 * Owns interactive PTY sessions and provides two complementary APIs:
 *
 *  1. **Streaming**: spawn a PTY, push raw bytes to subscribers (xterm UI),
 *     accept user input via `write()`. Standard interactive terminal model.
 *
 *  2. **Programmatic exec**: `exec(id, command)` writes a command to the PTY
 *     wrapped with a unique end-of-command sentinel, captures the output
 *     between write and sentinel, and resolves with stdout + exit code.
 *     This is what the agent uses when it wants to "drive" the user's
 *     visible terminal — the user sees the command being typed and the
 *     output streaming live, while the agent gets a structured result back.
 *
 * Concurrency note: only one `exec()` may be in flight per session. Callers
 * must serialize their requests; this class throws if exec is invoked while
 * another exec is still pending on the same session.
 */
export class TerminalManager extends EventEmitter {
  private sessions = new Map<string, SessionEntry>();

  spawn(options: TerminalSpawnOptions = {}): TerminalSession {
    const id = randomUUID();
    const shell = options.command || this.defaultShell();
    const cwd = options.cwd || os.homedir();
    const cols = options.cols || DEFAULT_COLS;
    const rows = options.rows || DEFAULT_ROWS;
    const env = {
      ...process.env,
      ...(options.env || {}),
      // Keep the prompt simple so the sentinel-based capture is robust.
      // Users can override in their dotfiles; only TERM/LANG are forced.
      TERM: 'xterm-256color',
      LANG: process.env.LANG || 'en_US.UTF-8',
      STEERABLE_AGENT_PTY: '1',
    } as Record<string, string>;

    const proc = pty.spawn(shell, options.args || [], {
      name: 'xterm-256color',
      cols,
      rows,
      cwd,
      env,
    });

    const entry: SessionEntry = {
      id,
      proc,
      shell,
      cwd,
      cols,
      rows,
      capture: null,
      replay: '',
    };
    this.sessions.set(id, entry);

    proc.onData((chunk: string) => {
      this.emit('data', id, chunk);
      // Keep a small replay buffer so a terminal window opened *after* output
      // has already started can show what was missed.
      entry.replay += chunk;
      if (entry.replay.length > REPLAY_BUFFER_BYTES) {
        entry.replay = entry.replay.slice(-REPLAY_BUFFER_BYTES);
      }
      const cap = entry.capture;
      if (!cap) return;
      cap.buffer += chunk;
      if (cap.buffer.length > MAX_EXEC_OUTPUT_BYTES) {
        cap.buffer = cap.buffer.slice(-MAX_EXEC_OUTPUT_BYTES);
      }
      // Match the OSC sentinel (invisible to the user, present in raw bytes).
      // The shell-echoed command source contains the literal text
      //   printf '\033]9999;__DP_END_<n>__:%s\007' "$?"
      // where `\033` is 4 ASCII chars (backslash + 0 + 3 + 3), NOT an ESC
      // byte. So that echo can never accidentally satisfy this regex — only
      // the printf *output* contains a real ESC byte.
      const sentinelRe = buildSentinelRegex(cap.nonce);
      const m = sentinelRe.exec(cap.buffer);
      if (m && m.index !== undefined) {
        // Keep everything before the OSC, plus a synthetic textual marker
        // that exec() can parse without re-doing OSC matching. The OSC bytes
        // themselves are dropped — they're invisible UI noise.
        const before = cap.buffer.slice(0, m.index);
        const captured = `${before}\n${SENTINEL_PREFIX}${cap.nonce}__:${m[1]}__`;
        entry.capture = null;
        cap.resolve(captured);
      }
    });

    proc.onExit((event) => {
      const { exitCode, signal } = event;
      this.emit('exit', id, exitCode, typeof signal === 'number' ? String(signal) : null);
      this.sessions.delete(id);
    });

    const session: TerminalSession = { id, title: shell, pid: proc.pid, cols, rows };
    this.emit('spawned', session);
    log.info('[terminal] spawned', { id, shell, pid: proc.pid, cwd });
    return session;
  }

  list(): TerminalSession[] {
    return Array.from(this.sessions.values()).map(e => ({
      id: e.id,
      title: e.shell,
      pid: e.proc.pid,
      cols: e.cols,
      rows: e.rows,
    }));
  }

  primarySession(): TerminalSession | null {
    const first = this.sessions.values().next().value;
    if (!first) return null;
    return {
      id: first.id,
      title: first.shell,
      pid: first.proc.pid,
      cols: first.cols,
      rows: first.rows,
    };
  }

  ensurePrimary(options: TerminalSpawnOptions = {}): TerminalSession {
    return this.primarySession() || this.spawn(options);
  }

  /**
   * Snapshot of recent PTY output, used to "catch up" a renderer that
   * subscribed after the session started producing output.
   */
  getReplayBuffer(id: string): string {
    return this.sessions.get(id)?.replay ?? '';
  }

  write(id: string, data: string): boolean {
    const entry = this.sessions.get(id);
    if (!entry) return false;
    entry.proc.write(data);
    return true;
  }

  resize(id: string, cols: number, rows: number): boolean {
    const entry = this.sessions.get(id);
    if (!entry) return false;
    if (cols > 0 && rows > 0) {
      try {
        entry.proc.resize(cols, rows);
        entry.cols = cols;
        entry.rows = rows;
        return true;
      } catch (err) {
        log.warn('[terminal] resize failed', { id, cols, rows, err: String(err) });
        return false;
      }
    }
    return false;
  }

  kill(id: string): boolean {
    const entry = this.sessions.get(id);
    if (!entry) return false;
    try {
      entry.proc.kill();
    } catch (err) {
      log.warn('[terminal] kill failed', { id, err: String(err) });
    }
    this.sessions.delete(id);
    return true;
  }

  killAll(): void {
    for (const id of Array.from(this.sessions.keys())) this.kill(id);
  }

  /**
   * Execute a single command in the visible PTY and wait for it to finish.
   * The user sees the command typed and output streaming live. The returned
   * result is parsed from the captured stream by stripping the sentinel.
   *
   * Caller must ensure no other exec() is pending on the same session.
   */
  async exec(
    id: string,
    command: string,
    timeoutMs: number = DEFAULT_EXEC_TIMEOUT_MS
  ): Promise<TerminalExecResult> {
    const entry = this.sessions.get(id);
    if (!entry) {
      return {
        code: -1,
        stdout: '',
        stderr: `terminal session ${id} not found`,
      };
    }
    if (entry.capture) {
      throw new Error(`terminal ${id} is busy with another exec`);
    }
    const trimmed = command.trim();
    if (!trimmed) {
      return { code: 0, stdout: '', stderr: '' };
    }

    // Short-ish nonce keeps the echoed command line readable. 12 hex chars
    const nonce = randomUUID().slice(0, 12);
    const startMs = Date.now();

    // Use OSC 9999 private control sequence to carry exit code back cleanly.
    // Conformant terminals ignore it, so the user sees no escape sequence.
    //
    // For command chaining, we need a robust fallback.
    // Unix:
    //   cmd ; printf '\x1b]9999;__DP_END_%s__:%s\x07' "nonce" "$?"
    // Win (cmd):
    //   cmd & printf ... %errorlevel% (or echo-based fallback)
    const formattedCmd = (() => {
      const isWin = process.platform === 'win32';
      if (isWin) {
        // cmd.exe doesn't natively have a reliable printf/echo that outputs pure binary
        // escape codes without standard external utilities. However, our local-executor
        // can query/wrap. For pure PTY, we append an OSC payload.
        // We write the command, followed by our custom control sequence:
        // We can leverage the fact that powershell/cmd might be running. If it's cmd,
        // we can output the sentinel.
        return `${trimmed} & echo \u001b]9999;${SENTINEL_PREFIX}${nonce}__:%errorlevel%\u0007\r\n`;
      } else {
        // Unix (zsh/bash). We use printf because it's a shell builtin.
        // Format of the OSC control code: \u001b]9999;__DP_END_<nonce>__:<exitCode>\u0007
        // We also force a final trailing newline so the terminal prompt is clean.
        return (
          `${trimmed} ; ` +
          `printf '\\033]9999;${SENTINEL_PREFIX}${nonce}__:%s\\007\\n' "$?" \n`
        );
      }
    })();

    const capturePromise = new Promise<string>((resolve) => {
      entry.capture = {
        buffer: '',
        nonce,
        resolve,
      };
    });

    // Send the wrapped command to the PTY
    entry.proc.write(formattedCmd);

    return new Promise<TerminalExecResult>((resolve) => {
      const timer = setTimeout(() => {
        if (entry.capture && entry.capture.nonce === nonce) {
          const rawOutput = entry.capture.buffer;
          entry.capture = null;
          resolve({
            code: -1,
            stdout: stripAnsi(rawOutput),
            stderr: `Command timed out after ${timeoutMs}ms`,
          });
        }
      }, timeoutMs);

      void capturePromise.then((captured) => {
        clearTimeout(timer);
        const parsed = this.parseCapturedOutput(captured, nonce, startMs);
        resolve(parsed);
      });
    });
  }

  private parseCapturedOutput(
    captured: string,
    nonce: string,
    _startMs: number
  ): TerminalExecResult {
    const sentinelStr = `${SENTINEL_PREFIX}${nonce}__:`;
    const sentinelIndex = captured.lastIndexOf(sentinelStr);
    if (sentinelIndex === -1) {
      return {
        code: -1,
        stdout: stripAnsi(captured),
        stderr: 'Failed to find exit code sentinel in captured PTY output',
      };
    }

    const beforeSentinel = captured.slice(0, sentinelIndex);
    const suffix = captured.slice(sentinelIndex + sentinelStr.length);
    const endMatch = suffix.match(/^((-?\d+))__/);
    const exitCode = endMatch ? parseInt(endMatch[1], 10) : -1;

    // Clean up carriage returns, normalize line endings, strip ANSI color sequences.
    const cleanStdout = stripAnsi(beforeSentinel)
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .trim();

    return {
      code: exitCode,
      stdout: cleanStdout,
      stderr: exitCode === 0 ? '' : `Command exited with non-zero code ${exitCode}`,
    };
  }

  private defaultShell(): string {
    if (process.platform === 'win32') {
      return 'powershell.exe';
    }
    return process.env.SHELL || '/bin/bash';
  }
}
