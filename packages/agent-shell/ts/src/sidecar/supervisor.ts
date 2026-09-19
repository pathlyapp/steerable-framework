/**
 * Supervises the steerable-sidecar Python subprocess.
 *
 * Responsibilities:
 *  - locate the bundled portable Python runtime (or accept an override),
 *  - spawn the sidecar with stdin/stdout/stderr pipes,
 *  - wait for the `__SIDECAR_READY__` marker on stderr before resolving start(),
 *  - parse JSON-RPC frames and dispatch them to pending requests / handlers,
 *  - run a periodic `system.ping` health-check and auto-restart on failure,
 *  - kill the process when the Electron app quits.
 */

import { execFile, spawn } from 'node:child_process';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

import { onAppWillQuit, offAppWillQuit } from '../runtime.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

import { resolveWinSpawnHelperPath } from './reverse-spawn.js';
import {
  SidecarBootError,
  SidecarMethodError,
  SidecarSandboxUnavailableError,
  SidecarShutdownError,
} from './errors.js';
import type {
  SidecarApplyEditsResult,
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
  SidecarHealthSnapshot,
  SidecarMethodOptions,
  SidecarModelCatalog,
  SidecarReverseHandler,
  SidecarSandboxPosture,
  SidecarSessionBranches,
  SidecarSessionForkOutcome,
  SidecarSessionForkResult,
  SidecarSessionMessages,
  SidecarSessionTree,
  SidecarSkillModule,
  SidecarStartOptions,
  SidecarChildEvent,
  SidecarStreamChunk,
  SidecarStreamDone,
  SidecarStreamError,
  SidecarToolResult,
} from './types.js';

const READY_PREFIX = '__SIDECAR_READY__:';
const DEFAULT_BOOT_TIMEOUT_MS = 15_000;
const DEFAULT_HEALTH_INTERVAL_MS = 5_000;
const DEFAULT_RESTART_AFTER_FAILED_PINGS = 3;
export const RUST_SIDECAR_ENV = 'STEERABLE_RUST_SIDECAR';
export const RUST_SIDECAR_BIN_ENV = 'STEERABLE_RUST_SIDECAR_BIN';
// Only /usr/bin/sandbox-exec is trusted — a PATH-relative lookup could
// resolve to an attacker-planted binary (codex's rule).
const SEATBELT_EXECUTABLE = '/usr/bin/sandbox-exec';
const execFileAsync = promisify(execFile);

/**
 * Tool names carried by a `tool.list` reply.
 *
 * The sidecar answers with OpenAI function-call descriptors —
 * `{ type: 'function', function: { name, description, parameters } }` — so the
 * name sits one level in. Reading a top-level `name` yields undefined for every
 * entry, which reads as "the sidecar registered nothing" rather than as a
 * decoding error.
 */
export function toolNamesFromDescriptors(listed: unknown): string[] {
  if (!Array.isArray(listed)) return [];
  const names: string[] = [];
  for (const entry of listed) {
    if (!entry || typeof entry !== 'object') continue;
    const fn = (entry as { function?: unknown }).function;
    if (!fn || typeof fn !== 'object') continue;
    const name = (fn as { name?: unknown }).name;
    if (typeof name === 'string' && name) names.push(name);
  }
  return names;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
  timer: NodeJS.Timeout;
}

/**
 * `SidecarSupervisor.start()` 的 boot 失败错误上携带的实例：后台 restart
 * 循环仍在重试，宿主可监听其 'ready' 事件处理迟到就绪。
 */
export interface SidecarBootFailure {
  supervisor?: SidecarSupervisor;
}

export class SidecarSupervisor extends EventEmitter {
  /** Last layer-1 refuse when start() threw (settings page still needs a posture). */
  static lastSpawnRefusal: SidecarSandboxPosture | null = null;

  private child: ChildProcessWithoutNullStreams | null = null;
  private readyHealth: SidecarHealthSnapshot | null = null;
  private sandboxPosture: SidecarSandboxPosture | null = null;
  private nextRequestId = 1;
  private pending = new Map<number, PendingRequest>();
  private stdoutBuffer = '';
  private stderrBuffer = '';
  private healthTimer: NodeJS.Timeout | null = null;
  private failedPings = 0;
  private shuttingDown = false;
  private quitListener: (() => void) | null = null;
  private reverseHandlers = new Map<string, SidecarReverseHandler>();

  private constructor(private readonly options: SidecarStartOptions) {
    super();
  }

  /** Spawn the sidecar and wait for the ready marker. */
  static async start(options: SidecarStartOptions = {}): Promise<SidecarSupervisor> {
    const supervisor = new SidecarSupervisor(options);
    try {
      await supervisor.boot();
      SidecarSupervisor.lastSpawnRefusal = null;
      return supervisor;
    } catch (err) {
      if (err instanceof SidecarSandboxUnavailableError) {
        SidecarSupervisor.lastSpawnRefusal = err.posture;
      }
      // boot 失败后 child 的 exit→restart 循环仍在后台重试（例如另一个
      // 宿主暂时持有 sessions.lock，冲突会自愈）。把实例挂在错误上，宿主
      // 可监听 'ready' 在迟到就绪时补注册——否则 sidecar 活着但
      // getSidecarSupervisor() 永远 null，聊天一直 503。
      (err as SidecarBootFailure).supervisor = supervisor;
      throw err;
    }
  }

  /** Round-trip a JSON-RPC method call. */
  async call<T = unknown>(
    method: string,
    params?: unknown,
    options: SidecarMethodOptions = {},
  ): Promise<T> {
    const child = this.requireChild();
    const id = this.nextRequestId++;
    const frame = JSON.stringify({ jsonrpc: '2.0', id, method, params });

    return new Promise<T>((resolve, reject) => {
      const timeout = options.timeoutMs ?? 60_000;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new SidecarMethodError(
          `sidecar method ${method} timed out after ${timeout}ms`,
          -32000,
          'timeout',
          undefined,
        ));
      }, timeout);
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timer,
      });
      child.stdin.write(frame + '\n', (err) => {
        if (err) {
          clearTimeout(timer);
          this.pending.delete(id);
          reject(new SidecarMethodError(
            `failed to write to sidecar: ${err.message}`,
            -32000,
            'transport_closed',
            undefined,
          ));
        }
      });
    });
  }

  /** Convenience: list tools registered on the sidecar. */
  async listTools(): Promise<unknown[]> {
    return await this.call<unknown[]>('tool.list');
  }

  /**
   * The gateway's live model catalog (`models.list`): ids the configured
   * gateway actually accepts, joined with models.dev capabilities. The
   * host passes the user's configured baseUrl/apiKey explicitly — the
   * sidecar process env does not carry the app's settings. Fetch failures
   * come back as `catalogStatus: 'offline'`/`'stale'` in the payload, not
   * as RPC errors, so the picker can badge instead of breaking.
   */
  async listModels(params: {
    baseUrl?: string;
    apiKey?: string;
    refresh?: boolean;
    provider?: string;
  } = {}): Promise<SidecarModelCatalog> {
    return await this.call<SidecarModelCatalog>('models.list', params, {
      // The sidecar fetches the gateway's /models over the network; clear
      // that bound or the caller sees a transport timeout instead of the
      // payload's own offline status.
      timeoutMs: 20_000,
    });
  }

  /** Names advertised by the sidecar's registry, decoded from `tool.list`. */
  async listToolNames(): Promise<string[]> {
    return toolNamesFromDescriptors(await this.listTools());
  }

  /** Convenience: invoke a tool by name. */
  async invokeTool(
    name: string,
    args: Record<string, unknown> = {},
    extra: {
      consentGranted?: boolean;
      context?: Record<string, unknown>;
      // Network-read tools (web_fetch/web_search) bound their own request time
      // sidecar-side; the RPC deadline must clear that bound or the caller sees
      // a transport timeout instead of the tool's own bounded error.
      timeoutMs?: number;
    } = {},
  ): Promise<SidecarToolResult> {
    return await this.call<SidecarToolResult>(
      'tool.invoke',
      {
        name,
        arguments: args,
        consentGranted: Boolean(extra.consentGranted),
        context: extra.context,
      },
      { timeoutMs: extra.timeoutMs },
    );
  }

  /** Convenience: ping for a health snapshot. */
  async ping(): Promise<SidecarHealthSnapshot> {
    return await this.call<SidecarHealthSnapshot>('system.ping', null, { timeoutMs: 5_000 });
  }

  /**
   * W6-1 single source of truth: run the structured-edit algorithm in the
   * sidecar (Python `file_edit.apply_edits`) on caller-supplied content. The
   * host keeps all file I/O (read / version check / atomic write); only the
   * locate-and-replace surgery crosses the wire so the desktop and the
   * headless / ACP workspace tools share one implementation.
   */
  async applyEdits(params: {
    content: string;
    edits: Array<{ oldText: string; newText: string }>;
    filePath?: string;
  }): Promise<SidecarApplyEditsResult> {
    return await this.call<SidecarApplyEditsResult>(
      'workspace.apply_edits',
      params,
      { timeoutMs: 15_000 },
    );
  }

  /**
   * Skills single source of truth: parse + select SKILL.md modules in the
   * sidecar (Python `skills.py`) from host-supplied roots. Returns both layers
   * with bodies; the host applies its own layer filter / budget / name lookup.
   */
  async listSkills(params: {
    roots: string[];
    conditions?: string[];
    exclude?: string[];
    ignoreConditions?: boolean;
  }): Promise<SidecarSkillModule[]> {
    const result = await this.call<{ skills: SidecarSkillModule[] }>(
      'skills.list',
      params,
      { timeoutMs: 15_000 },
    );
    return result.skills;
  }

  /**
   * Run a streaming chat completion through the sidecar's `agent.chat.stream`
   * method. Subscribes to `stream.chunk` / `stream.done` / `stream.error`
   * notifications, demuxes them by `streamId`, and surfaces them through the
   * supplied callbacks.
   *
   * Returns the `streamId` so callers can correlate cancel requests.
   */
  async streamChat(
    request: SidecarChatStreamRequest,
    handlers: SidecarChatStreamHandlers,
  ): Promise<string> {
    const result = await this.call<{ streamId: string }>(
      'agent.chat.stream',
      request,
      { timeoutMs: request.startTimeoutMs ?? 30_000 },
    );
    const streamId = result.streamId;

    const onChunk = (params: unknown) => {
      const payload = params as { streamId?: string };
      if (!payload || payload.streamId !== streamId) return;
      handlers.onChunk?.(payload as SidecarStreamChunk);
    };
    const onDone = (params: unknown) => {
      const payload = params as { streamId?: string };
      if (!payload || payload.streamId !== streamId) return;
      this.off('stream.chunk', onChunk);
      this.off('stream.done', onDone);
      this.off('stream.error', onError);
      this.off('agent.child', onChild);
      handlers.onDone?.(payload as SidecarStreamDone);
    };
    const onError = (params: unknown) => {
      const payload = params as { streamId?: string };
      if (!payload || payload.streamId !== streamId) return;
      this.off('stream.chunk', onChunk);
      this.off('stream.done', onDone);
      this.off('stream.error', onError);
      this.off('agent.child', onChild);
      handlers.onError?.(payload as SidecarStreamError);
    };

    // P3.1 orchestration lifecycle: `agent.child` notifications carry the
    // same streamId, demuxed here so the turn driver can surface child
    // spawn/complete/fail/interrupt/resume in the UI.
    const onChild = (params: unknown) => {
      const payload = params as { streamId?: string } & SidecarChildEvent;
      if (!payload || payload.streamId !== streamId) return;
      handlers.onChildEvent?.(payload);
    };

    this.on('stream.chunk', onChunk);
    this.on('stream.done', onDone);
    this.on('stream.error', onError);
    this.on('agent.child', onChild);
    return streamId;
  }

  /** Best-effort cancel a sidecar stream by id. */
  async cancelChat(streamId: string): Promise<void> {
    try {
      await this.call('agent.chat.cancel', { streamId }, { timeoutMs: 2_000 });
    } catch {
      /* best effort — sidecar may have already finished */
    }
  }

  /**
   * W5-2: fork a durable record without running a turn — the non-destructive
   * regenerate primitive.
   *
   * Reports why a fork did not happen instead of collapsing every cause to a
   * single falsy value. The caller's fallback destroys the reply's UI row and
   * lets the next turn append into the same record, so a declined address and a
   * failed request are not interchangeable — see {@link SidecarSessionForkOutcome}.
   */
  async forkSession(params: {
    recordId: string;
    beforeLastUser?: boolean;
    beforeUserIndex?: number;
    newRecordId?: string;
    label?: string;
  }): Promise<SidecarSessionForkOutcome> {
    try {
      const fork = await this.call<SidecarSessionForkResult>(
        'agent.session.fork',
        params,
        { timeoutMs: 10_000 },
      );
      return { ok: true, fork };
    } catch (err) {
      // `invalid_request` is the sidecar rejecting the address itself; anything
      // else (transport, timeout, sidecar fault) never reached that judgement.
      const method = err instanceof SidecarMethodError ? err : undefined;
      return {
        ok: false,
        declined: method?.kind === 'invalid_request',
        reason: method
          ? `${method.kind ?? 'error'} (${method.code}): ${method.message}`
          : err instanceof Error
            ? err.message
            : String(err),
      };
    }
  }

  /**
   * W1.2.1: branch-family view of a record (lineage + direct children).
   * Soft-fail null when the sidecar has no such record — the caller renders
   * "no branches" rather than an error.
   */
  async sessionBranches(recordId: string): Promise<SidecarSessionBranches | null> {
    try {
      return await this.call<SidecarSessionBranches>(
        'agent.session.branches',
        { recordId },
        { timeoutMs: 10_000 },
      );
    } catch {
      return null;
    }
  }

  /**
   * Session tree: full branch family containing a record, expanded from
   * the family root (`agent.session.tree`). Unlike {@link sessionBranches}
   * (lineage + direct children only), this sees cousins and deeper
   * descendants — the activation guard and the tree modal both need it.
   * Soft-fail null when the sidecar has no such record — the caller
   * renders "no branches" rather than an error.
   */
  async sessionTree(recordId: string): Promise<SidecarSessionTree | null> {
    try {
      return await this.call<SidecarSessionTree>(
        'agent.session.tree',
        { recordId },
        { timeoutMs: 10_000 },
      );
    } catch {
      return null;
    }
  }

  /**
   * W1.2.1: projected transcript of a record (post-boundary visible span) —
   * the read path for rendering a branch after a switch. Soft-fail null.
   */
  async sessionMessages(recordId: string): Promise<SidecarSessionMessages | null> {
    try {
      return await this.call<SidecarSessionMessages>(
        'agent.session.messages',
        { recordId },
        { timeoutMs: 15_000 },
      );
    } catch {
      return null;
    }
  }

  /**
   * Inject a user message into a running CoreLoop turn (mid-turn steering).
   * Soft-fails (`{ ok: false }`) when the turn already ended — the caller
   * should then send the message as a normal new turn instead.
   */
  async steerChat(streamId: string, content: string): Promise<boolean> {
    try {
      const result = await this.call<{ ok: boolean; reason?: string }>(
        'agent.chat.steer',
        { streamId, content },
        { timeoutMs: 2_000 },
      );
      return result.ok === true;
    } catch {
      return false;
    }
  }

  /**
   * Register a host-side handler for a reverse (sidecar -> host) request.
   *
   * When the sidecar hosts the agent loop but a tool must execute in the
   * Electron process (shell, filesystem, MCP), the sidecar sends a reverse
   * `tool.invoke` request; the handler registered for that method runs the
   * real tool and its return value is sent back as the JSON-RPC result.
   */
  onReverseRequest(method: string, handler: SidecarReverseHandler): void {
    this.reverseHandlers.set(method, handler);
  }

  /** Returns the most recent ready snapshot collected at boot. */
  getBootSnapshot(): SidecarHealthSnapshot | null {
    return this.readyHealth;
  }

  /**
   * W4-3: the layer-1 (sidecar process sandbox) posture recorded at
   * spawn-plan time. This is the value the renderer reads (via
   * `GET /api/v2/sidecar/sandbox-posture`) to disclose confined / opt-out
   * / refused-start. A refused start also lands on `lastSpawnRefusal`.
   */
  getSandboxPosture(): SidecarSandboxPosture | null {
    return this.sandboxPosture;
  }

  /** Graceful shutdown. */
  async shutdown(): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    this.stopHealthTimer();
    if (this.quitListener) {
      offAppWillQuit(this.quitListener);
      this.quitListener = null;
    }
    const child = this.child;
    if (!child) return;
    try {
      await this.call('system.shutdown', null, { timeoutMs: 2_000 });
    } catch { /* sidecar might already be terminating */ }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch { /* noop */ }
        resolve();
      }, 2_000);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.child = null;
    this.failPending(new SidecarShutdownError('sidecar shut down'));
  }

  // ------------------------------------------------------------------
  // Internal: boot
  // ------------------------------------------------------------------

  private async boot(): Promise<void> {
    const rustBin = rustSidecarEnabled()
      ? resolveRustSidecarBin(this.options.rustSidecarBin)
      : undefined;
    let spawnPlan: { command: string; args: string[]; env?: NodeJS.ProcessEnv };
    if (rustBin) {
      spawnPlan = await this.resolveSandboxedSpawn(rustBin, this.options.args ?? []);
    } else {
      const py = this.resolvePythonBinary();
      const entry = this.options.entryModule ?? 'steerable_sidecar';
      const args = ['-m', entry, ...(this.options.args ?? [])];
      spawnPlan = await this.resolveSandboxedSpawn(py, args);
    }
    const child = spawn(spawnPlan.command, spawnPlan.args, {
      cwd: this.options.cwd,
      env: { ...process.env, ...this.options.env, ...spawnPlan.env },
      stdio: ['pipe', 'pipe', 'pipe'],
    }) as ChildProcessWithoutNullStreams;
    this.child = child;
    this.attachListeners(child);

    try {
      this.readyHealth = await this.waitForReady(this.options.bootTimeoutMs ?? DEFAULT_BOOT_TIMEOUT_MS);
    } catch (err) {
      try { child.kill('SIGKILL'); } catch { /* noop */ }
      this.child = null;
      throw err;
    }

    this.installAppQuitHook();
    this.startHealthTimer();
    this.emit('ready', this.readyHealth);
  }

  /**
   * Wrap the sidecar spawn in the platform's process sandbox.
   *
   * macOS Seatbelt, Linux bwrap/Landlock, Windows restricted-token
   * passthrough. Any failure refuses start — confinement requested means
   * the process does not run unsandboxed. Explicit opt-out
   * (`sandbox: false` / `STEERABLE_SIDECAR_SANDBOX=0`) is the only
   * unconfined path. Every exit records `sandboxPosture`.
   */
  private async resolveSandboxedSpawn(
    command: string,
    args: string[],
  ): Promise<{ command: string; args: string[]; env?: NodeJS.ProcessEnv }> {
    const plain = { command, args };
    const enabled = this.options.sandbox ?? process.env.STEERABLE_SIDECAR_SANDBOX !== '0';
    if (!enabled) {
      this.sandboxPosture = {
        backend: 'none',
        enforcement: 'none',
        reason: this.options.sandbox === false ? 'disabled_by_option' : 'disabled_by_env',
      };
      return plain;
    }

    const steerableDir = join(homedir(), '.steerable');
    mkdirSync(steerableDir, { recursive: true });
    // 除默认 ~/.steerable 外的 writable roots（storage path 在 userData/tmp
    // 等外部目录时由宿主传入）。macOS Seatbelt 与 Linux bwrap 都要求 root
    // 已存在，这里统一先建好；Windows helper 同样要求已存在。
    const writableRoots = [steerableDir];
    for (const root of this.options.sandboxWritableRoots ?? []) {
      if (!root || root === steerableDir || writableRoots.includes(root)) continue;
      mkdirSync(root, { recursive: true });
      writableRoots.push(root);
    }
    const allowedHosts =
      this.options.sandboxAllowedHosts ??
      (process.env.STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS ?? '')
        .split(',')
        .map((h) => h.trim())
        .filter(Boolean);
    const webEgress = Boolean(this.options.sandboxWebEgress) && allowedHosts.length > 0;
    // 3.1b: egress-proxy 模式下 web_fetch 的 SSRF 预检在沙箱内解析 DNS，
    // 但真正的出口走代理。Seatbelt 只放行 resolver socket，不放行 *:80/443。
    const allowResolver = Boolean(this.options.sandboxAllowResolver) && allowedHosts.length > 0 && !webEgress;

    if (process.platform === 'darwin') {
      return this.wrapSeatbelt(command, args, writableRoots, allowedHosts, webEgress, allowResolver);
    }
    if (process.platform === 'linux') {
      return this.wrapLinux(command, args, writableRoots);
    }
    if (process.platform === 'win32') {
      return this.wrapWindows(command, args, writableRoots);
    }
    const posture: SidecarSandboxPosture = {
      backend: 'none',
      enforcement: 'none',
      reason: 'platform_unsupported',
    };
    this.sandboxPosture = posture;
    throw new SidecarSandboxUnavailableError(
      `sandbox: no process confinement on ${process.platform}; refusing unsandboxed spawn`,
      posture,
    );
  }

  private refuse(posture: SidecarSandboxPosture, message: string, cause?: unknown): never {
    this.sandboxPosture = posture;
    this.options.onLogLine?.(message);
    throw new SidecarSandboxUnavailableError(message, posture, cause);
  }

  private async wrapSeatbelt(
    command: string,
    args: string[],
    writableRoots: string[],
    allowedHosts: string[],
    webEgress: boolean,
    allowResolver: boolean,
  ): Promise<{ command: string; args: string[]; env?: NodeJS.ProcessEnv }> {
    if (!existsSync(SEATBELT_EXECUTABLE)) {
      this.refuse(
        { backend: 'none', enforcement: 'none', reason: 'seatbelt_missing' },
        'sandbox: /usr/bin/sandbox-exec missing; refusing unsandboxed spawn',
      );
    }
    try {
      const flags = [
        ...writableRoots.flatMap((root) => ['--writable-root', root]),
        ...allowedHosts.flatMap((h) => ['--allow-host', h]),
        ...(webEgress ? ['--allow-web-egress'] : []),
        ...(allowResolver ? ['--allow-resolver'] : []),
      ];
      const rustBin = rustSidecarEnabled()
        ? resolveRustSidecarBin(this.options.rustSidecarBin)
        : undefined;
      const { stdout } = rustBin
        ? await execFileAsync(rustBin, ['sandbox', 'profile', ...flags], { timeout: 10_000 })
        : await execFileAsync(
            command,
            ['-m', 'steerable_sidecar.sandbox', 'profile', ...flags],
            { timeout: 10_000 },
          );
      const profile = stdout.trim();
      if (!profile.includes('(deny default)')) {
        throw new Error('generated profile is not a Seatbelt policy');
      }
      this.options.onLogLine?.(
        `sandbox: Seatbelt active (writes: ${writableRoots.join(', ')} + scratch` +
          (allowedHosts.length ? `; egress: ${allowedHosts.join(', ')}` : '; egress: open') +
          (webEgress ? ' + web tools: DNS, any host on 80/443' : '') +
          (allowResolver ? ' + resolver only (web tools egress via the per-host proxy)' : '') +
          ')',
      );
      this.sandboxPosture = { backend: 'seatbelt', enforcement: 'partial', reason: 'active' };
      return {
        command: SEATBELT_EXECUTABLE,
        args: ['-p', profile, command, ...args],
        env: {
          PYTHONDONTWRITEBYTECODE: '1',
          // macOS denies a nested sandbox_apply once the outer profile allows
          // outbound network, so a layer-1-confined sidecar cannot wrap its own
          // run_code child. The marker tells the sidecar to let that child
          // inherit this layer-1 boundary instead of failing the nested wrap.
          // Linux (bwrap/Landlock stack) and Windows don't set it — run_code
          // keeps its dedicated layer-2 there.
          STEERABLE_SIDECAR_CONFINED: '1',
        },
      };
    } catch (err) {
      if (err instanceof SidecarSandboxUnavailableError) throw err;
      this.refuse(
        { backend: 'none', enforcement: 'none', reason: 'profile_failed' },
        `sandbox: profile generation failed; refusing unsandboxed spawn: ${String(err)}`,
        err,
      );
    }
  }

  private async wrapLinux(
    command: string,
    args: string[],
    writableRoots: string[],
  ): Promise<{ command: string; args: string[]; env?: NodeJS.ProcessEnv }> {
    // 3.1c：框架的 linux-wrap 只接受 --writable-root / --no-network——
    // bwrap 的 allowed_hosts 仅是接口兼容（不强制），Landlock 根本没有
    // per-host egress。所以 Linux 的按主机管控不在 layer-1：egress-proxy
    // 模式下靠 sidecar 进程的 HTTPS_PROXY env（boot.ts 注入，bwrap/landlock
    // 都透传环境）+ sidecar 应用层域名名单强制，网络命名空间保持共享。
    // 这里如实记录，不假装接上了实际不强制的参数。
    const egressViaProxy = this.options.env?.STEERABLE_EGRESS_CONFINED === '1';
    const rustBin = rustSidecarEnabled()
      ? resolveRustSidecarBin(this.options.rustSidecarBin)
      : undefined;
    const wrapArgs = rustBin
      ? [
          'sandbox',
          'linux-wrap',
          ...writableRoots.flatMap((root) => ['--writable-root', root]),
          '--',
          command,
          ...args,
        ]
      : [
          '-m',
          'steerable_sidecar.sandbox',
          'linux-wrap',
          ...writableRoots.flatMap((root) => ['--writable-root', root]),
          '--',
          command,
          ...args,
        ];
    try {
      const { stdout } = await execFileAsync(rustBin ?? command, wrapArgs, { timeout: 15_000 });
      const plan = JSON.parse(stdout.trim()) as {
        argv?: unknown;
        backend?: unknown;
        enforcement?: unknown;
      };
      if (!Array.isArray(plan.argv) || plan.argv.length < 1 || typeof plan.argv[0] !== 'string') {
        throw new Error('linux-wrap did not return an argv');
      }
      const argv = plan.argv.map(String);
      const backend =
        plan.backend === 'landlock' ? 'landlock' : plan.backend === 'bwrap' ? 'bwrap' : null;
      if (!backend) {
        throw new Error(`linux-wrap unknown backend ${String(plan.backend)}`);
      }
      this.options.onLogLine?.(
        `sandbox: ${backend} active (writes: ${writableRoots.join(', ')} + scratch; ` +
          (egressViaProxy
            ? 'egress: per-host via the egress proxy (HTTPS_PROXY env) + app-layer domain list; layer-1 network shared (bwrap/landlock have no per-host pinning)'
            : 'egress: open → partial') +
          ')',
      );
      this.sandboxPosture = { backend, enforcement: 'partial', reason: 'active' };
      return {
        command: argv[0],
        args: argv.slice(1),
        env: { PYTHONDONTWRITEBYTECODE: '1' },
      };
    } catch (err) {
      if (err instanceof SidecarSandboxUnavailableError) throw err;
      this.refuse(
        { backend: 'none', enforcement: 'none', reason: 'wrap_failed' },
        `sandbox: Linux process wrap failed; refusing unsandboxed spawn: ${String(err)}`,
        err,
      );
    }
  }

  private wrapWindows(
    command: string,
    args: string[],
    writableRoots: string[],
  ): { command: string; args: string[]; env?: NodeJS.ProcessEnv } {
    const helper = resolveWinSpawnHelperPath();
    if (!helper) {
      this.refuse(
        { backend: 'none', enforcement: 'none', reason: 'helper_missing' },
        'sandbox: win-spawn-helper.exe not found; refusing unsandboxed sidecar spawn',
      );
    }
    const steerableDir = writableRoots[0];
    const extraRoots = writableRoots.slice(1);
    // 受限令牌下系统临时目录不可写，Python tempfile.gettempdir() 探测不到
    // 可用目录会直接 FileNotFoundError。把子进程的 TEMP/TMP 指到 writable
    // root 内，保证 sidecar 的 spill/临时文件始终有处可写。
    const confinedTmp = join(steerableDir, 'tmp');
    mkdirSync(confinedTmp, { recursive: true });
    this.options.onLogLine?.(
      'sandbox: windows-restricted-token active (writes: ~/.steerable' +
        (extraRoots.length ? ` + ${extraRoots.join(', ')}` : '') +
        '; network not enforced → partial)',
    );
    this.sandboxPosture = {
      backend: 'windows-restricted-token',
      enforcement: 'partial',
      reason: 'active',
    };
    return {
      command: helper,
      args: [
        '--passthrough',
        '--writable-root',
        steerableDir,
        ...extraRoots.flatMap((root) => ['--writable-root', root]),
        '--',
        command,
        ...args,
      ],
      env: {
        PYTHONDONTWRITEBYTECODE: '1',
        TEMP: confinedTmp,
        TMP: confinedTmp,
        // A child spawned by a restricted-token process runs under the same
        // restricted token (CreateProcess inherits the caller's primary
        // token), so the layer-1 wrap already confines run_code's child by
        // inheritance — there is no layer-2 backend on Windows to nest.
        // The marker lets the sidecar report that honestly as
        // backend=inherited / enforcement=partial instead of refusing with
        // sandbox_unavailable.
        STEERABLE_SIDECAR_CONFINED: '1',
      },
    };
  }

  private attachListeners(child: ChildProcessWithoutNullStreams): void {
    child.stdout.setEncoding('utf-8');
    child.stderr.setEncoding('utf-8');
    child.stdout.on('data', (chunk: string) => this.handleStdoutChunk(chunk));
    child.stderr.on('data', (chunk: string) => this.handleStderrChunk(chunk));
    child.on('exit', (code, signal) => {
      this.emit('exit', { code, signal });
      this.failPending(new SidecarShutdownError(
        `sidecar exited (code=${code ?? 'null'}, signal=${signal ?? 'null'})`,
      ));
      if (!this.shuttingDown) {
        this.scheduleRestart('child exited unexpectedly');
      }
    });
    child.on('error', (err) => {
      this.emit('error', err);
    });
  }

  private installAppQuitHook(): void {
    const hook = () => {
      void this.shutdown();
    };
    this.quitListener = hook;
    onAppWillQuit(hook);
  }

  private async waitForReady(timeoutMs: number): Promise<SidecarHealthSnapshot> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup();
        reject(new SidecarBootError(
          `timed out waiting for sidecar ready marker after ${timeoutMs}ms`,
        ));
      }, timeoutMs);
      const onReady = (snapshot: SidecarHealthSnapshot) => {
        cleanup();
        resolve(snapshot);
      };
      const onExitEarly = (info: { code: number | null; signal: NodeJS.Signals | null }) => {
        cleanup();
        reject(new SidecarBootError(
          `sidecar exited before ready (code=${info.code ?? 'null'}, signal=${info.signal ?? 'null'})`,
        ));
      };
      const cleanup = () => {
        clearTimeout(timer);
        this.off('__ready_marker__', onReady);
        this.off('exit', onExitEarly);
      };
      this.once('__ready_marker__', onReady);
      this.once('exit', onExitEarly);
    });
  }

  // ------------------------------------------------------------------
  // Internal: stream parsing
  // ------------------------------------------------------------------

  private handleStdoutChunk(chunk: string): void {
    this.stdoutBuffer += chunk;
    let nl = this.stdoutBuffer.indexOf('\n');
    while (nl !== -1) {
      const line = this.stdoutBuffer.slice(0, nl).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(nl + 1);
      if (line) this.handleStdoutLine(line);
      nl = this.stdoutBuffer.indexOf('\n');
    }
  }

  private handleStderrChunk(chunk: string): void {
    this.stderrBuffer += chunk;
    let nl = this.stderrBuffer.indexOf('\n');
    while (nl !== -1) {
      const line = this.stderrBuffer.slice(0, nl);
      this.stderrBuffer = this.stderrBuffer.slice(nl + 1);
      this.handleStderrLine(line);
      nl = this.stderrBuffer.indexOf('\n');
    }
  }

  private handleStderrLine(line: string): void {
    if (line.startsWith(READY_PREFIX)) {
      try {
        const payload = JSON.parse(line.slice(READY_PREFIX.length)) as SidecarHealthSnapshot;
        this.emit('__ready_marker__', payload);
      } catch (err) {
        this.emit('error', new SidecarBootError(
          `failed to parse ready marker: ${(err as Error).message}`,
          err,
        ));
      }
      return;
    }
    this.options.onLogLine?.(line);
  }

  private handleStdoutLine(line: string): void {
    let payload: any;
    try {
      payload = JSON.parse(line);
    } catch {
      this.emit('error', new Error(`malformed sidecar frame: ${line.slice(0, 200)}`));
      return;
    }
    // A frame with both an id and a method is a *request* from the sidecar
    // (reverse channel) — not a response to one of ours. Serve it.
    if (payload.id !== undefined && typeof payload.method === 'string') {
      void this.handleReverseRequest(payload);
      return;
    }
    if (payload.id !== undefined) {
      this.dispatchResponse(payload);
      return;
    }
    if (typeof payload.method === 'string') {
      this.dispatchNotification(payload.method, payload.params);
    }
  }

  private async handleReverseRequest(payload: {
    id: string | number;
    method: string;
    params?: unknown;
  }): Promise<void> {
    const handler = this.reverseHandlers.get(payload.method);
    let response: Record<string, unknown>;
    if (!handler) {
      response = {
        jsonrpc: '2.0',
        id: payload.id,
        error: {
          code: -32601,
          kind: 'method_not_found',
          message: `no host handler for reverse method '${payload.method}'`,
        },
      };
    } else {
      try {
        const result = await handler(payload.params);
        response = { jsonrpc: '2.0', id: payload.id, result: result ?? null };
      } catch (err) {
        response = {
          jsonrpc: '2.0',
          id: payload.id,
          error: {
            code: -32603,
            kind: 'internal',
            message: (err as Error).message,
          },
        };
      }
    }
    this.writeFrame(response);
  }

  private writeFrame(frame: Record<string, unknown>): void {
    const child = this.child;
    if (!child) return;
    child.stdin.write(JSON.stringify(frame) + '\n', (err) => {
      if (err) this.emit('error', new Error(`failed to write reverse response: ${err.message}`));
    });
  }

  private dispatchResponse(payload: { id: number; result?: unknown; error?: { code: number; message: string; kind?: string; data?: unknown } }): void {
    const pending = this.pending.get(payload.id);
    if (!pending) return;
    this.pending.delete(payload.id);
    clearTimeout(pending.timer);
    if (payload.error) {
      pending.reject(new SidecarMethodError(
        payload.error.message,
        payload.error.code,
        payload.error.kind,
        payload.error.data,
      ));
      return;
    }
    pending.resolve(payload.result);
  }

  private dispatchNotification(method: string, params: unknown): void {
    if (method === 'stream.chunk') {
      this.options.onStreamChunk?.(params);
    }
    if (method === 'lifecycle.shutdown') {
      this.emit('lifecycle:shutdown', params);
    }
    this.emit(method, params);
  }

  // ------------------------------------------------------------------
  // Internal: health + restart
  // ------------------------------------------------------------------

  private startHealthTimer(): void {
    const interval = this.options.healthIntervalMs ?? DEFAULT_HEALTH_INTERVAL_MS;
    if (interval <= 0) return;
    this.healthTimer = setInterval(() => {
      void this.runHealthCheck();
    }, interval);
  }

  private stopHealthTimer(): void {
    if (this.healthTimer) {
      clearInterval(this.healthTimer);
      this.healthTimer = null;
    }
  }

  private async runHealthCheck(): Promise<void> {
    if (this.shuttingDown || !this.child) return;
    try {
      await this.ping();
      this.failedPings = 0;
    } catch (err) {
      this.failedPings += 1;
      this.emit('health:fail', { count: this.failedPings, error: err });
      const threshold = this.options.restartAfterFailedPings ?? DEFAULT_RESTART_AFTER_FAILED_PINGS;
      if (this.failedPings >= threshold) {
        this.failedPings = 0;
        this.scheduleRestart(`failed ${threshold} consecutive pings`);
      }
    }
  }

  private scheduleRestart(reason: string): void {
    if (this.shuttingDown) return;
    this.emit('restart:scheduled', { reason });
    this.stopHealthTimer();
    setTimeout(() => {
      void this.restart(reason);
    }, 250);
  }

  private async restart(reason: string): Promise<void> {
    if (this.shuttingDown) return;
    this.emit('restart:starting', { reason });
    if (this.child) {
      try { this.child.kill('SIGTERM'); } catch { /* noop */ }
    }
    this.child = null;
    try {
      await this.boot();
      this.emit('restart:succeeded', { reason });
    } catch (err) {
      this.emit('restart:failed', { reason, error: err });
    }
  }

  // ------------------------------------------------------------------
  // Internal: helpers
  // ------------------------------------------------------------------

  private requireChild(): ChildProcessWithoutNullStreams {
    if (!this.child) {
      throw new SidecarShutdownError('sidecar is not running');
    }
    return this.child;
  }

  private failPending(err: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.pending.clear();
  }

  private resolvePythonBinary(): string {
    return resolveSidecarPython(this.options.pythonExecutable);
  }
}

/**
 * Resolve the sidecar's Python interpreter: explicit option →
 * STEERABLE_SIDECAR_PYTHON → bundled python-runtime → sibling framework
 * venv (dev layout) → system python. Module-level so the egress-proxy
 * launcher (W1.3.3) spawns the same interpreter the sidecar uses.
 */
export function resolveSidecarPython(pythonExecutable?: string): string {
  if (pythonExecutable) {
    return pythonExecutable;
  }
  const explicit = process.env.STEERABLE_SIDECAR_PYTHON;
  if (explicit && existsSync(explicit)) return explicit;

  const platformTag = (() => {
    switch (process.platform) {
      case 'darwin':
        return process.arch === 'arm64' ? 'darwin-arm64' : 'darwin-x64';
      case 'win32':
        return 'win32-x64';
      default:
        return 'linux-x64';
    }
  })();
  const binaryName = process.platform === 'win32' ? 'python.exe' : 'python3';

  // build_sidecar.py 的产物布局是 <platform>/python/<exe>（Windows：
  // python/python.exe；POSIX：python/bin/python3，见 build_sidecar.py 的
  // python_binary()）。保留无 python/ 层的旧布局候选做向后兼容。
  const runtimeBases: string[] = [];
  try {
    const resourcesPath = (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
    if (resourcesPath) {
      runtimeBases.push(join(resourcesPath, 'python-runtime'));
    }
  } catch { /* not in Electron */ }
  runtimeBases.push(join(__dirname, '..', '..', 'python-runtime'));

  const candidates: string[] = [];
  for (const base of runtimeBases) {
    candidates.push(join(base, platformTag, 'python', binaryName));
    candidates.push(join(base, platformTag, 'python', 'bin', binaryName));
    candidates.push(join(base, platformTag, binaryName));
    candidates.push(join(base, platformTag, 'bin', binaryName));
  }

  // Dev-layout convenience: the framework repo root .venv (uv sync). The
  // shell lives inside the framework repo (packages/agent-shell/ts), so the
  // venv is an in-repo ancestor — no sibling-checkout probing. Without this,
  // a default-on sidecar on a dev machine falls through to system python3,
  // which lacks steerable_sidecar, and the router silently degrades to the
  // TS loop. Guarded by existsSync — absent in packaged builds, where the
  // bundled python-runtime candidates above win.
  // Windows 的 venv 解释器在 Scripts/python.exe（没有 bin/python3）。
  // 本模块在两个平面执行：源码（vitest，ts/src/sidecar/）与编译产物
  // （ts/dist/sidecar/）——到框架仓库根分别是四层与五层，两个候选都压入，
  // 由下方 existsSync 挑出存在的那个。
  for (const up of [
    join('..', '..', '..', '..'),
    join('..', '..', '..', '..', '..'),
  ]) {
    const frameworkVenv = join(__dirname, up, '.venv');
    candidates.push(join(frameworkVenv, 'bin', 'python3'));
    if (process.platform === 'win32') {
      candidates.push(join(frameworkVenv, 'Scripts', 'python.exe'));
    }
  }

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  // Fallback to system python (developer machines).
  return process.platform === 'win32' ? 'python' : 'python3';
}

function envFlag(name: string): boolean {
  const raw = (process.env[name] ?? '').trim().toLowerCase();
  return raw === '1' || raw === 'true' || raw === 'yes' || raw === 'on';
}

export function rustSidecarEnabled(): boolean {
  return envFlag(RUST_SIDECAR_ENV);
}

/**
 * Resolve the optional Rust sidecar binary. Missing binary with the flag
 * on is a silent fallback to Python (`python -m steerable_sidecar`).
 */
export function resolveRustSidecarBin(explicit?: string): string | undefined {
  if (explicit) {
    return existsSync(explicit) ? explicit : undefined;
  }
  const fromEnv = process.env[RUST_SIDECAR_BIN_ENV];
  if (fromEnv) {
    return existsSync(fromEnv) ? fromEnv : undefined;
  }
  const exe = process.platform === 'win32' ? 'steerable-sidecar.exe' : 'steerable-sidecar';
  const relatives = [
    join('..', '..', '..', '..', 'sidecar', 'rs', 'target', 'debug', exe),
    join('..', '..', '..', '..', '..', 'sidecar', 'rs', 'target', 'debug', exe),
    join('..', '..', '..', '..', 'sidecar', 'rs', 'target', 'release', exe),
    join('..', '..', '..', '..', '..', 'sidecar', 'rs', 'target', 'release', exe),
  ];
  for (const rel of relatives) {
    const candidate = join(__dirname, rel);
    if (existsSync(candidate)) return candidate;
  }
  return undefined;
}
