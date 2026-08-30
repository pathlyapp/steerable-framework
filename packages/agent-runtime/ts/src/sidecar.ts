/**
 * `SidecarProcess` — owns the embedded Python sidecar lifecycle so callers
 * never write subprocess management:
 *
 *   spawn → wait `lifecycle.ready` → `system.ping` → serve requests
 *   unexpected exit → bounded auto-restart with backoff
 *   close() → `system.shutdown` → wait → SIGTERM → SIGKILL (escalating)
 *
 * Requests made while the process is down reject fast with
 * `SidecarNotReadyError`; the caller decides whether to retry after the
 * next `ready` event. This mirrors the fail-loud posture of the rest of
 * the framework — a wedged sidecar surfaces as an error, not a hang.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import { EventEmitter } from 'node:events';
import {
  JsonRpcPeer,
  JsonRpcTransportClosedError,
} from './jsonrpc.js';

export class SidecarNotReadyError extends Error {
  constructor(message = 'sidecar is not running') {
    super(message);
    this.name = 'SidecarNotReadyError';
  }
}

export class SidecarStartError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SidecarStartError';
  }
}

export interface SidecarRestartPolicy {
  /** Max consecutive restarts before the process is left dead. Default 3. */
  maxRestarts?: number;
  /** Base backoff in ms; attempt n waits `base * 2^(n-1)`. Default 250. */
  backoffMs?: number;
}

export interface SidecarProcessOptions {
  /**
   * Python interpreter used to launch the sidecar. Default: env
   * `STEERABLE_PYTHON`, else `python3`. The interpreter must have
   * `steerable-sidecar` installed (or be run from a venv that does).
   */
  python?: string;
  /**
   * Entry module; defaults to `steerable_sidecar`. Pass `null` to invoke the
   * interpreter directly on `args` (no `-m <module>` prefix) — used by tests
   * that drive a fake sidecar under `node`.
   */
  entryModule?: string | null;
  /** Extra CLI flags appended after `-m <entryModule>`. */
  args?: string[];
  /** Working directory for the sidecar process. */
  cwd?: string;
  /** Extra environment merged over `process.env`. */
  env?: NodeJS.ProcessEnv;
  /** Restart policy for unexpected exits. Pass `false` to disable. */
  restart?: SidecarRestartPolicy | false;
  /** Ms to wait for `lifecycle.ready` before failing the start. Default 15s. */
  readyTimeoutMs?: number;
  /** Ms to wait for graceful exit before SIGTERM. Default 5s. */
  shutdownGraceMs?: number;
  /** Reverse-channel handler (sidecar → host, e.g. `tool.invoke`). */
  onRequest?: (method: string, params: unknown) => Promise<unknown>;
  /** Sidecar-originated notifications (stream.chunk, agent.child, …). */
  onNotification?: (method: string, params: unknown) => void;
  /** stderr lines from the sidecar land here (default: swallowed). */
  onStderr?: (line: string) => void;
}

export interface SidecarReadyInfo {
  version?: string;
  protocolVersion?: string;
  pid?: number;
  listenInfo?: unknown;
}

const SIGKILL_GRACE_MS = 1_000;

export class SidecarProcess extends EventEmitter {
  private readonly options: SidecarProcessOptions;
  private child: ChildProcess | null = null;
  private peer: JsonRpcPeer | null = null;
  private readyInfo: SidecarReadyInfo | null = null;
  /** True between close() and actual exit — an expected exit, no restart. */
  private closing = false;
  /** True once a boot fully succeeded; only booted processes earn restarts. */
  private booted = false;
  private restarts = 0;
  private startPromise: Promise<void> | null = null;
  /** Internal deferred resolved when the current boot sees lifecycle.ready. */
  private readyDeferred: {
    resolve: () => void;
    reject: (err: Error) => void;
  } | null = null;

  constructor(options: SidecarProcessOptions = {}) {
    super();
    this.options = options;
  }

  /** True once `lifecycle.ready` + `system.ping` have completed. */
  get isReady(): boolean {
    return this.peer !== null;
  }

  /** Params of the `lifecycle.ready` notification, once received. */
  get ready(): SidecarReadyInfo | null {
    return this.readyInfo;
  }

  /** PID of the live child, if any. */
  get pid(): number | undefined {
    return this.child?.pid;
  }

  /**
   * Spawn the sidecar and wait for readiness. A manual start resets the
   * consecutive-restart budget; the auto-restart path deliberately does not
   * (a crash-looping sidecar must exhaust `maxRestarts`, not loop forever).
   */
  start(): Promise<void> {
    this.restarts = 0;
    return this.boot();
  }

  private boot(): Promise<void> {
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.spawnOnce().finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  /**
   * Graceful stop: `system.shutdown`, wait for exit, escalate to SIGTERM
   * then SIGKILL. Resolves when the process is gone (or was never up).
   */
  async close(): Promise<void> {
    this.closing = true;
    // A restart boot may be in flight — wait for it so we don't leave a
    // live child behind after close() returns.
    if (this.startPromise) {
      await this.startPromise.catch(() => undefined);
    }
    const child = this.child;
    if (!child) return;
    const exited = new Promise<void>((resolve) => {
      child.once('exit', () => resolve());
    });
    try {
      if (this.peer) {
        await this.peer
          .request('system.shutdown', undefined, { timeoutMs: 5_000 })
          .catch(() => undefined);
      }
    } finally {
      const graceMs = this.options.shutdownGraceMs ?? 5_000;
      const grace = await raceTimeout(exited, graceMs);
      if (!grace) {
        child.kill('SIGTERM');
        const terminated = await raceTimeout(exited, SIGKILL_GRACE_MS);
        if (!terminated) child.kill('SIGKILL');
      }
      await exited.catch(() => undefined);
      this.teardownPeer('sidecar closed');
      this.child = null;
    }
  }

  /** JSON-RPC request against the live sidecar; rejects fast when down. */
  request<TResult = unknown>(
    method: string,
    params?: unknown,
    options?: { timeoutMs?: number | null },
  ): Promise<TResult> {
    const peer = this.peer;
    if (!peer) return Promise.reject(new SidecarNotReadyError());
    return peer.request<TResult>(method, params, options);
  }

  private async spawnOnce(): Promise<void> {
    const python =
      this.options.python ?? process.env.STEERABLE_PYTHON ?? 'python3';
    const entry =
      this.options.entryModule === null
        ? null
        : (this.options.entryModule ?? 'steerable_sidecar');
    const args = [
      ...(entry === null ? [] : ['-m', entry]),
      ...(this.options.args ?? []),
    ];
    const child = spawn(python, args, {
      cwd: this.options.cwd,
      env: { ...process.env, ...this.options.env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.child = child;

    child.stderr?.setEncoding('utf8');
    let stderrBuf = '';
    child.stderr?.on('data', (chunk: string) => {
      stderrBuf += chunk;
      let nl: number;
      while ((nl = stderrBuf.indexOf('\n')) !== -1) {
        const line = stderrBuf.slice(0, nl);
        stderrBuf = stderrBuf.slice(nl + 1);
        if (line.trim().length > 0) this.options.onStderr?.(line);
      }
    });

    const peer = new JsonRpcPeer({
      write: (line) => {
        child.stdin?.write(line);
      },
      onNotification: (method, params) => this.handleNotification(method, params),
      onRequest: this.options.onRequest,
    });

    child.stdout?.setEncoding('utf8');
    child.stdout?.on('data', (chunk: string) => peer.feed(chunk));
    child.on('exit', (code, signal) => this.handleExit(code, signal));
    child.on('error', (err) => {
      this.teardownPeer(`sidecar spawn error: ${err.message}`);
    });

    const readyTimeoutMs = this.options.readyTimeoutMs ?? 15_000;
    const readySignal = new Promise<void>((resolve, reject) => {
      this.readyDeferred = { resolve, reject };
    });
    const timer = setTimeout(() => {
      this.readyDeferred?.reject(
        new SidecarStartError(
          `sidecar did not emit lifecycle.ready within ${readyTimeoutMs}ms`,
        ),
      );
    }, readyTimeoutMs);
    child.once('exit', (code) => {
      this.readyDeferred?.reject(
        new SidecarStartError(`sidecar exited during boot (code ${String(code)})`),
      );
    });
    // The peer only becomes the serving peer after ready + ping, so a
    // half-booted process never answers requests. A failed boot kills the
    // child — callers must not leak a process they never got a handle to.
    try {
      await readySignal;
    } catch (err) {
      child.kill('SIGKILL');
      throw err;
    } finally {
      clearTimeout(timer);
      this.readyDeferred = null;
    }
    try {
      await peer.request('system.ping', undefined, { timeoutMs: 5_000 });
    } catch (err) {
      child.kill('SIGKILL');
      throw new SidecarStartError(
        `sidecar failed post-ready ping: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    this.peer = peer;
    this.booted = true;
    // 'ready' means fully serving: listeners may issue requests immediately.
    this.emit('ready', this.readyInfo);
  }

  private handleNotification(method: string, params: unknown): void {
    if (method === 'lifecycle.ready') {
      this.readyInfo = (params ?? {}) as SidecarReadyInfo;
      this.readyDeferred?.resolve();
      return;
    }
    if (method === 'lifecycle.shutdown') {
      this.emit('shutdown', params);
      return;
    }
    this.options.onNotification?.(method, params);
  }

  private handleExit(code: number | null, signal: NodeJS.Signals | null): void {
    const wasClosing = this.closing;
    const wasBooted = this.booted;
    this.booted = false;
    this.teardownPeer(`sidecar exited (code ${String(code)}, signal ${String(signal)})`);
    this.child = null;
    this.readyInfo = null;
    this.emit('exit', code, signal);
    // Only a process that fully booted earns a restart — a boot failure is
    // almost always deterministic (bad interpreter, missing package), and
    // retrying it just delays the caller's error.
    if (wasClosing || !wasBooted) return;

    const policy = this.options.restart;
    if (policy === false) return;
    const maxRestarts = policy?.maxRestarts ?? 3;
    const backoffMs = policy?.backoffMs ?? 250;
    if (this.restarts >= maxRestarts) {
      this.emit('dead', code, signal);
      return;
    }
    this.restarts += 1;
    const delay = backoffMs * 2 ** (this.restarts - 1);
    this.emit('restart', this.restarts, delay);
    setTimeout(() => {
      if (this.closing) return;
      this.boot().catch((err: unknown) => {
        this.emit(
          'restart-failed',
          err instanceof Error ? err : new Error(String(err)),
        );
      });
    }, delay);
  }

  private teardownPeer(reason: string): void {
    if (!this.peer) return;
    this.peer.close(reason);
    this.peer = null;
  }
}

function raceTimeout(p: Promise<void>, ms: number): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), ms);
    p.then(() => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}

export { JsonRpcTransportClosedError };
