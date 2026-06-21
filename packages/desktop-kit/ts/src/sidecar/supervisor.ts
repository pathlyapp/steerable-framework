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

import { spawn } from 'node:child_process';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { app } from 'electron';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

import { SidecarBootError, SidecarMethodError, SidecarShutdownError } from './errors.js';
import type {
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
  SidecarHealthSnapshot,
  SidecarMethodOptions,
  SidecarStartOptions,
  SidecarStreamChunk,
  SidecarStreamDone,
  SidecarStreamError,
  SidecarToolResult,
} from './types.js';

const READY_PREFIX = '__SIDECAR_READY__:';
const DEFAULT_BOOT_TIMEOUT_MS = 15_000;
const DEFAULT_HEALTH_INTERVAL_MS = 5_000;
const DEFAULT_RESTART_AFTER_FAILED_PINGS = 3;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
  timer: NodeJS.Timeout;
}

export class SidecarSupervisor extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private readyHealth: SidecarHealthSnapshot | null = null;
  private nextRequestId = 1;
  private pending = new Map<number, PendingRequest>();
  private stdoutBuffer = '';
  private stderrBuffer = '';
  private healthTimer: NodeJS.Timeout | null = null;
  private failedPings = 0;
  private shuttingDown = false;
  private quitListener: (() => void) | null = null;

  private constructor(private readonly options: SidecarStartOptions) {
    super();
  }

  /** Spawn the sidecar and wait for the ready marker. */
  static async start(options: SidecarStartOptions = {}): Promise<SidecarSupervisor> {
    const supervisor = new SidecarSupervisor(options);
    await supervisor.boot();
    return supervisor;
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

  /** Convenience: invoke a tool by name. */
  async invokeTool(
    name: string,
    args: Record<string, unknown> = {},
    extra: { consentGranted?: boolean; context?: Record<string, unknown> } = {},
  ): Promise<SidecarToolResult> {
    return await this.call<SidecarToolResult>('tool.invoke', {
      name,
      arguments: args,
      consentGranted: Boolean(extra.consentGranted),
      context: extra.context,
    });
  }

  /** Convenience: ping for a health snapshot. */
  async ping(): Promise<SidecarHealthSnapshot> {
    return await this.call<SidecarHealthSnapshot>('system.ping', null, { timeoutMs: 5_000 });
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
      handlers.onDone?.(payload as SidecarStreamDone);
    };
    const onError = (params: unknown) => {
      const payload = params as { streamId?: string };
      if (!payload || payload.streamId !== streamId) return;
      this.off('stream.chunk', onChunk);
      this.off('stream.done', onDone);
      this.off('stream.error', onError);
      handlers.onError?.(payload as SidecarStreamError);
    };

    this.on('stream.chunk', onChunk);
    this.on('stream.done', onDone);
    this.on('stream.error', onError);
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

  /** Returns the most recent ready snapshot collected at boot. */
  getBootSnapshot(): SidecarHealthSnapshot | null {
    return this.readyHealth;
  }

  /** Graceful shutdown. */
  async shutdown(): Promise<void> {
    if (this.shuttingDown) return;
    this.shuttingDown = true;
    this.stopHealthTimer();
    if (this.quitListener) {
      try { app.removeListener('will-quit', this.quitListener); } catch { /* noop */ }
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
    const py = this.resolvePythonBinary();
    const entry = this.options.entryModule ?? 'steerable_sidecar';
    const args = ['-m', entry, ...(this.options.args ?? [])];

    const child = spawn(py, args, {
      cwd: this.options.cwd,
      env: { ...process.env, ...this.options.env },
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
    try { app.on('will-quit', hook); } catch { /* tests may run outside Electron */ }
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
    if (payload.id !== undefined) {
      this.dispatchResponse(payload);
      return;
    }
    if (typeof payload.method === 'string') {
      this.dispatchNotification(payload.method, payload.params);
    }
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
    if (this.options.pythonExecutable) {
      return this.options.pythonExecutable;
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

    const candidates: string[] = [];
    try {
      const resourcesPath = (process as NodeJS.Process & { resourcesPath?: string }).resourcesPath;
      if (resourcesPath) {
        candidates.push(join(resourcesPath, 'python-runtime', platformTag, binaryName));
        candidates.push(join(resourcesPath, 'python-runtime', platformTag, 'bin', binaryName));
      }
    } catch { /* not in Electron */ }
    candidates.push(join(__dirname, '..', '..', 'python-runtime', platformTag, binaryName));
    candidates.push(join(__dirname, '..', '..', 'python-runtime', platformTag, 'bin', binaryName));

    for (const candidate of candidates) {
      if (existsSync(candidate)) return candidate;
    }
    // Fallback to system python (developer machines).
    return process.platform === 'win32' ? 'python' : 'python3';
  }
}
