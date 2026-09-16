import { EventEmitter } from 'events';
import os from 'os';
import { randomUUID } from 'crypto';
import log from 'electron-log';
import * as pty from 'node-pty';
import { adaptCommandForPowerShell } from './shell-adapt.js';

export type TerminalShell = 'zsh' | 'bash' | 'sh' | 'powershell.exe' | 'cmd.exe';

export interface TerminalSpawnOptions {
  shell?: TerminalShell;
  cwd?: string;
  env?: Record<string, string>;
  cols?: number;
  rows?: number;
}

export interface TerminalSession {
  id: string;
  shell: string;
  pid: number;
  cwd: string;
  cols: number;
  rows: number;
}

export interface TerminalExecResult {
  success: boolean;
  exitCode: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  durationMs: number;
}

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
// Grace window for the sentinel after a timeout-triggered SIGINT — the
// recovery probe runs as soon as the shell returns to a prompt, so this
// only needs to cover shell scheduling, not real work.
const KILL_GRACE_MS = 3_000;
// Let the SIGINT echo settle before typing the recovery probe line.
const KILL_PROBE_DELAY_MS = 400;
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
  capture: {
    buffer: string;
    nonce: string;
    /**
     * Set when a timeout triggered killOnTimeout: after SIGINT, zsh aborts
     * the whole command list (the wrapper's sentinel printf never runs), so
     * a standalone recovery probe line is written to the fresh prompt. Its
     * sentinel proves the shell is processing commands again.
     */
    recoveryNonce?: string;
    /** Set once the head of `buffer` has been dropped to respect the byte cap. */
    truncated: boolean;
    resolve: (out: { text: string; truncated: boolean; interrupted?: boolean }) => void;
    /** Lets `onExit` fail a pending exec immediately instead of waiting out its timeout. */
    reject: (err: Error) => void;
  } | null;
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
    const shell = options.shell || this.defaultShell();
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
      DEEPPATH_AGENT_PTY: '1',
    } as Record<string, string>;

    // cmd.exe needs delayed expansion (`/v:on`) so `exec()` can read a
    // freshly-set `!ERRORLEVEL!` for a command joined on the same line via
    // `&` — see the comment in `exec()`. `%ERRORLEVEL%` would otherwise be
    // substituted once when the whole compound line is parsed (i.e. before
    // the preceding command has even run), so it always reads the *previous*
    // command's exit code instead of the one we just ran.
    const args = shell.toLowerCase().includes('cmd.exe') ? ['/v:on'] : [];

    const proc = pty.spawn(shell, args, {
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
        // Keep the tail (where the sentinel will land) and remember that we
        // dropped bytes — `body.length` after this can never exceed the cap,
        // so callers must rely on this flag rather than re-deriving it from
        // the final output length.
        cap.buffer = cap.buffer.slice(-MAX_EXEC_OUTPUT_BYTES);
        cap.truncated = true;
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
        // A shell that continues the wrapper's `; printf` list after the
        // timeout SIGINT (bash) resolves through this normal sentinel path
        // rather than the recovery probe — it is still an interruption.
        cap.resolve({
          text: captured,
          truncated: cap.truncated,
          interrupted: cap.recoveryNonce ? true : undefined,
        });
        return;
      }
      // Recovery probe after a timeout SIGINT: its sentinel means the shell
      // is back at a prompt. Resolve the interrupted exec with exit 130.
      if (cap.recoveryNonce) {
        const recoveryRe = buildSentinelRegex(cap.recoveryNonce);
        const rm = recoveryRe.exec(cap.buffer);
        if (rm && rm.index !== undefined) {
          const before = cap.buffer.slice(0, rm.index);
          const captured = `${before}\n${SENTINEL_PREFIX}${cap.nonce}__:130__`;
          entry.capture = null;
          cap.resolve({ text: captured, truncated: cap.truncated, interrupted: true });
        }
      }
    });

    proc.onExit(({ exitCode, signal }) => {
      this.emit('exit', id, exitCode, typeof signal === 'number' ? String(signal) : null);
      // A pending exec() has nothing left to capture from — fail it right
      // away instead of leaving it to hang until its own timeout fires.
      if (entry.capture) {
        const pending = entry.capture;
        entry.capture = null;
        pending.reject(
          new Error(`terminal session ${id} exited (code=${exitCode}) while a command was still running`)
        );
      }
      this.sessions.delete(id);
    });

    // PowerShell：预定义哨兵函数，让 exec() 只需在命令尾追加 `; __dpe '<marker>'`
    // （~30 字符）。旧实现把整段 Write-Host 包装内联在命令行尾部，整行轻松超过
    // 终端列宽（100），ConPTY 会把回显折行甚至用光标重绘合并——下游的回显剥离
    // 因此时而残留回显碎片、时而把真实输出一起吞掉（flaky："expected '' to
    // contain 'hi'"）。函数体与旧内联逻辑一致：$? 在函数入口处仍持有上一条
    // 命令的执行状态，映射成 0/1 后以 OSC 9999 私有序列发出。
    if (shell.toLowerCase().includes('powershell') || shell.toLowerCase().includes('pwsh')) {
      proc.write(
        `function __dpe([string]$n){ $c=if($?){0}else{1}; Write-Host -NoNewline (([char]27)+']9999;'+$n+':'+$c+([char]7)) }\r`
      );
    }

    const session: TerminalSession = { id, shell, pid: proc.pid, cwd, cols, rows };
    this.emit('spawned', session);
    log.info('[terminal] spawned', { id, shell, pid: proc.pid, cwd });
    return session;
  }

  list(): TerminalSession[] {
    return Array.from(this.sessions.values()).map(e => ({
      id: e.id,
      shell: e.shell,
      pid: e.proc.pid,
      cwd: e.cwd,
      cols: e.cols,
      rows: e.rows,
    }));
  }

  primarySession(): TerminalSession | null {
    const first = this.sessions.values().next().value;
    if (!first) return null;
    return {
      id: first.id,
      shell: first.shell,
      pid: first.proc.pid,
      cwd: first.cwd,
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
   *
   * `killOnTimeout`: on timeout, send SIGINT (Ctrl-C) to the PTY instead of
   * just giving up on the capture. A timed-out command otherwise keeps
   * running in the shared visible terminal — every subsequent exec jams
   * behind it (typed into the busy terminal, never reaching a shell prompt)
   * and times out in cascade. The wrapper's `; printf <sentinel>` survives
   * the interrupt (SIGINT kills the foreground pipeline, not the shell's
   * command list), so after Ctrl-C we keep waiting briefly for the sentinel
   * and resolve with the interrupted exit code (130) instead of rejecting.
   * GUI-launch commands pass false: their timeout means "launched, still
   * running", and killing would terminate the app the user asked for.
   */
  async exec(
    id: string,
    command: string,
    timeoutMs: number = DEFAULT_EXEC_TIMEOUT_MS,
    killOnTimeout: boolean = false
  ): Promise<TerminalExecResult> {
    const entry = this.sessions.get(id);
    if (!entry) {
      return {
        success: false,
        exitCode: -1,
        stdout: '',
        stderr: `terminal session ${id} not found`,
        truncated: false,
        durationMs: 0,
      };
    }
    if (entry.capture) {
      throw new Error(`terminal ${id} is busy with another exec`);
    }
    let trimmed = command.trim();
    if (!trimmed) {
      return { success: true, exitCode: 0, stdout: '', stderr: '', truncated: false, durationMs: 0 };
    }
    // LLM 混写 cmd / bash 方言（&&、||、%VAR%）时，PowerShell 5.1 会直接报
    // 解析错误。可见终端默认就是 powershell.exe，这里与 LocalExecutor 用同一套
    // 确定性转换，保证两条执行路径行为一致。
    if (/powershell|pwsh/i.test(entry.shell)) {
      const adapted = adaptCommandForPowerShell(trimmed);
      if (adapted !== trimmed) {
        log.info('[terminal] adapted command for PowerShell', {
          before: trimmed.slice(0, 200),
          after: adapted.slice(0, 200),
        });
        trimmed = adapted;
      }
    }

    // Short-ish nonce keeps the echoed command line readable. 12 hex chars
    // = 48 bits of entropy, more than enough for "no two pending execs in
    // the same session" (which is already serialized).
    const nonce = randomUUID().replace(/-/g, '').slice(0, 12);
    const marker = `${SENTINEL_PREFIX}${nonce}__`;
    // Wrap the command so we always get an exit code marker even if it fails.
    // The marker is emitted as an OSC 999 private control sequence:
    //   ESC ] 9999 ; __DP_END_<nonce>__:<exitCode> BEL
    // We construct the wrapped command depending on the shell to support cross-platform (PowerShell, CMD, Bash/Zsh).
    const shellLower = entry.shell.toLowerCase();
    let wrapped = '';
    const ESC = '\u001b';
    const BEL = '\u0007';

    if (shellLower.includes('powershell') || shellLower.includes('pwsh')) {
      // 哨兵通过 spawn() 时预定义的 __dpe 函数发出（OSC 9999，PowerShell 5.1
      // 兼容，$? 对 cmdlet 也生效并映射为 0/1）。只追加 ~30 字符，避免整行
      // 超过终端列宽被 ConPTY 折行/重绘，破坏下游的回显剥离。
      wrapped = `${trimmed}; __dpe '${marker}'\r`;
    } else if (shellLower.includes('cmd.exe')) {
      // `%ERRORLEVEL%` on a `&`-joined line is expanded once when the whole
      // line is parsed, *before* `trimmed` has even run, so it always reports
      // the previous command's exit code. `!ERRORLEVEL!` (delayed expansion,
      // enabled at spawn time via `/v:on` — see `spawn()`) is instead
      // expanded at execution time of this specific token, after `trimmed`
      // has finished, which is what we actually want here.
      wrapped = `${trimmed} & node -e "process.stdout.write('\\x1b]9999;${marker}:' + process.argv[1] + '\\x07\\n')\" !ERRORLEVEL!\r`;
    } else {
      wrapped = `${trimmed}; printf '\\033]9999;${marker}:%s\\007\\n' "$?"\r`;
    }
    const start = Date.now();

    const { text: captured, truncated: capTruncated, interrupted } = await new Promise<{
      text: string;
      truncated: boolean;
      interrupted?: boolean;
    }>((resolve, reject) => {
      entry.capture = { buffer: '', nonce, truncated: false, resolve, reject };
      let killGraceTimer: ReturnType<typeof setTimeout> | null = null;
      const timer = setTimeout(() => {
        if (!entry.capture || entry.capture.nonce !== nonce) return;
        if (killOnTimeout) {
          // SIGINT the foreground pipeline. bash would continue the wrapper's
          // `; printf <sentinel>` list; zsh aborts the whole list, so arm a
          // recovery probe: a bare sentinel line the shell runs at the fresh
          // prompt, proving the terminal is usable again.
          const recoveryNonce = `R${nonce}`;
          entry.capture.recoveryNonce = recoveryNonce;
          entry.proc.write('\x03');
          setTimeout(() => {
            if (entry.capture && entry.capture.nonce === nonce) {
              entry.proc.write(
                `printf '\\033]9999;${SENTINEL_PREFIX}${recoveryNonce}__:0\\007\\n'\r`
              );
            }
          }, KILL_PROBE_DELAY_MS);
          killGraceTimer = setTimeout(() => {
            if (entry.capture && entry.capture.nonce === nonce) {
              entry.capture = null;
              reject(new Error(`exec timeout after ${timeoutMs}ms: ${trimmed.slice(0, 80)}`));
            }
          }, KILL_GRACE_MS);
          return;
        }
        entry.capture = null;
        reject(new Error(`exec timeout after ${timeoutMs}ms: ${trimmed.slice(0, 80)}`));
      }, timeoutMs);
      // Once the inner promise settles, clear the timer.
      const originalResolve = entry.capture.resolve;
      entry.capture.resolve = (out) => {
        clearTimeout(timer);
        if (killGraceTimer) clearTimeout(killGraceTimer);
        originalResolve(out);
      };
      const originalReject = entry.capture.reject;
      entry.capture.reject = (err) => {
        clearTimeout(timer);
        if (killGraceTimer) clearTimeout(killGraceTimer);
        originalReject(err);
      };
      entry.proc.write(wrapped);
    });

    const durationMs = Date.now() - start;

    // Parse the captured stream:
    //   <prompt>$ <wrapped-command>\r\n<output>...\n__DP_END_<nonce>__:<exitCode>__
    // The OSC sentinel that the shell actually wrote to the PTY has already
    // been intercepted in `onData` and replaced with this synthetic textual
    // marker, so plain regex matching is enough here.
    //
    // PTY output is full of ANSI/cursor escapes (prompt themes, ls colors,
    // bracketed-paste markers) plus carriage returns. We need to clean it up
    // before handing back to the agent — it'll be displayed as plain text.
    const cleaned = stripAnsi(captured)
      // node-pty often emits \r\n; agents/UI expect \n.
      .replace(/\r\n/g, '\n')
      // Bare \r (cursor-to-col-1) is meaningless in our line-based output.
      .replace(/\r/g, '')
      // Bracketed-paste markers some shells inject around input.
      .replace(/\u001B\[\?2004[lh]/g, '');

    // Match the synthetic textual marker that `onData` injected after the
    // real OSC sentinel was found in the raw stream. Anchor on a preceding
    // LF (or start-of-buffer) so we don't accidentally match the echoed
    // command source — the echo contains `__DP_END_<nonce>__:%s\007` (with
    // a literal `%s`), which can never satisfy `:(-?\\d+)__` anyway, but
    // anchoring is a cheap robustness win.
    const sentinelRegex = new RegExp(
      `(?:^|\\n)(${SENTINEL_PREFIX}${nonce}__:(-?\\d+)__)`
    );
    const m = cleaned.match(sentinelRegex);
    const exitCode = m ? Number.parseInt(m[2], 10) : -1;

    // Cut everything after (and including) the sentinel — m.index points at
    // the leading LF (or 0). Use the captured group's position so we strip
    // exactly the sentinel and keep the preceding output.
    let body = m
      ? cleaned.slice(0, cleaned.indexOf(m[1]))
      : cleaned;

    // Strip the echoed wrapped command line. We wrote
    //   `<cmd>; printf '\033]9999;__DP_END_<nonce>__:%s\007\n' "$?"`
    // and the shell echoes that whole thing back as literal characters
    // (`\033` is 4 ASCII chars in the echo, not a real ESC). Find the
    // unique nonce token in the echo and drop everything up to and
    // including its trailing newline.
    const echoToken = `${SENTINEL_PREFIX}${nonce}`;
    const echoEnd = body.indexOf(echoToken);
    if (echoEnd !== -1) {
      const nl = body.indexOf('\n', echoEnd);
      if (nl !== -1) {
        body = body.slice(nl + 1);
      } else {
        // ConPTY 有时用光标重绘代替真实换行，ANSI 剥离后回显尾部和真实输出
        // 会粘在同一"行"里。旧实现直接置空，把真实输出一起吞掉（flaky 回归：
        // "expected '' to contain 'hi'"）。这里改为：跳过 marker 后再剥掉各
        // shell 已知的回显收尾片段，保留其后的内容；剥不掉就原样保留——
        // 带一点回显噪音远好于凭空丢失输出。
        let tail = body.slice(echoEnd + echoToken.length);
        const knownEchoTails = [
          /^__'[ \t]*\n?/, //                                  PowerShell: __dpe '<marker>'
          /^__:' \+ process\.argv\[1\] \+ '[^\n]*?!ERRORLEVEL![ \t]*\n?/, // cmd.exe
          /^__:%s[^\n]*?"\$\?"[ \t]*\n?/, //                   POSIX printf
        ];
        for (const re of knownEchoTails) {
          const next = tail.replace(re, '');
          if (next !== tail) {
            tail = next;
            break;
          }
        }
        body = tail;
      }
    } else {
      // Fallback: the echo got line-wrapped or the sentinel name didn't appear
      // in echo. Try matching the user's command on the first line(s).
      const firstNl = body.indexOf('\n');
      if (firstNl !== -1 && body.slice(0, firstNl).includes(trimmed)) {
        body = body.slice(firstNl + 1);
      }
    }

    // Trim the trailing blank line that printf added before the sentinel.
    body = body.replace(/\n+$/, '');

    if (interrupted) {
      // Drop the recovery probe's echoed command line and the ^C artifact so
      // the agent only sees the interrupted command's own output.
      body = body
        .split('\n')
        .filter((line) => !line.includes(`${SENTINEL_PREFIX}R${nonce}`))
        .join('\n')
        .replace(/\^C\s*$/, '')
        .replace(/\n+$/, '');
    }

    // `body.length` can never exceed the cap by construction (see `onData`),
    // so we must rely on the flag raised while capturing rather than
    // re-deriving truncation from the final (already-capped) length.
    const truncated = capTruncated;

    return {
      success: exitCode === 0,
      exitCode,
      stdout: body,
      stderr: interrupted
        ? `[command exceeded ${timeoutMs}ms and was interrupted with SIGINT; the terminal is free again — re-run with a larger \`timeout\` if it needs longer]`
        : '',
      truncated,
      durationMs,
    };
  }

  private defaultShell(): TerminalShell {
    if (process.platform === 'win32') {
      // Default to PowerShell so the visible terminal matches the headless
      // executor (LocalExecutor also defaults to powershell.exe on Windows).
      // COMSPEC points at cmd.exe on a standard Windows install, which would
      // make agent-driven commands run under a different dialect than what the
      // skill prompt assumes — the mismatch is what made the agent issue
      // PowerShell cmdlets that silently failed under cmd. `cmd.exe` is still
      // explicitly selectable via TerminalSpawnOptions.shell.
      return 'powershell.exe';
    }
    const fromEnv = process.env.SHELL;
    if (fromEnv?.endsWith('/zsh')) return 'zsh';
    if (fromEnv?.endsWith('/bash')) return 'bash';
    return 'zsh';
  }
}
