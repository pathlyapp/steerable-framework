/**
 * Minimal line-delimited JSON-RPC 2.0 peer over a child process's
 * stdin/stdout, matching the framing in `docs/spec/sidecar.md`.
 *
 * Three inbound frame kinds are dispatched:
 *   - response (`id` + `result`/`error`)      → resolves the pending request
 *   - notification (`method`, no `id`)        → `onNotification`
 *   - reverse request (`id` + `method`)       → `onRequest`, and this client
 *     writes the response back (the sidecar calls host tools this way)
 *
 * The client owns no reconnection logic — that lives in `SidecarProcess`.
 * When the transport dies every pending request rejects with
 * `JsonRpcTransportClosedError` so callers fail fast instead of hanging.
 */

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: number | string;
  method: string;
  params?: unknown;
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
}

export interface JsonRpcSuccessResponse {
  jsonrpc: '2.0';
  id: number | string;
  result: unknown;
}

export interface JsonRpcErrorResponse {
  jsonrpc: '2.0';
  id: number | string;
  error: { code: number; message: string; data?: unknown };
}

export type JsonRpcInbound =
  | JsonRpcNotification
  | JsonRpcSuccessResponse
  | JsonRpcErrorResponse
  | (JsonRpcRequest & { id: number | string });

export class JsonRpcRemoteError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = 'JsonRpcRemoteError';
    this.code = code;
    this.data = data;
  }
}

export class JsonRpcTransportClosedError extends Error {
  constructor(message = 'json-rpc transport closed') {
    super(message);
    this.name = 'JsonRpcTransportClosedError';
  }
}

export interface JsonRpcPeerOptions {
  /** Bytes written here go to the peer (child stdin). */
  write: (line: string) => void;
  /** Sidecar-originated notifications (stream.chunk, lifecycle.*, …). */
  onNotification?: (method: string, params: unknown) => void;
  /**
   * Reverse-channel requests (sidecar → host, e.g. `tool.invoke`). Return
   * the result; throw to send a JSON-RPC error back. Unhandled methods
   * answer `-32601 Method not found`.
   */
  onRequest?: (method: string, params: unknown) => Promise<unknown>;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout> | null;
}

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

export class JsonRpcPeer {
  private readonly writeLine: (line: string) => void;
  private readonly onNotification: (method: string, params: unknown) => void;
  private readonly onRequest:
    | ((method: string, params: unknown) => Promise<unknown>)
    | null;
  private readonly pending = new Map<number | string, Pending>();
  private nextId = 1;
  private buffer = '';
  private closed = false;

  constructor(options: JsonRpcPeerOptions) {
    this.writeLine = options.write;
    this.onNotification = options.onNotification ?? (() => undefined);
    this.onRequest = options.onRequest ?? null;
  }

  /**
   * Feed raw stdout bytes. Frames are newline-delimited JSON; partial lines
   * are buffered until their terminator arrives.
   */
  feed(chunk: string): void {
    this.buffer += chunk;
    for (;;) {
      const nl = this.buffer.indexOf('\n');
      if (nl === -1) return;
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (line.length === 0) continue;
      let frame: JsonRpcInbound;
      try {
        frame = JSON.parse(line) as JsonRpcInbound;
      } catch {
        // Non-JSON stdout (a stray print inside the sidecar) must not kill
        // the channel — the protocol owns stdout, so anything else is noise.
        continue;
      }
      this.dispatch(frame);
    }
  }

  /** Send a request and await its result. Rejects on remote error or close. */
  request<TResult = unknown>(
    method: string,
    params?: unknown,
    options?: { timeoutMs?: number | null },
  ): Promise<TResult> {
    if (this.closed) {
      return Promise.reject(new JsonRpcTransportClosedError());
    }
    const id = this.nextId++;
    const timeoutMs =
      options && options.timeoutMs !== undefined
        ? options.timeoutMs
        : DEFAULT_REQUEST_TIMEOUT_MS;
    return new Promise<TResult>((resolve, reject) => {
      const timer =
        timeoutMs === null
          ? null
          : setTimeout(() => {
              if (this.pending.delete(id)) {
                reject(
                  new JsonRpcTransportClosedError(
                    `request ${method} timed out after ${timeoutMs}ms`,
                  ),
                );
              }
            }, timeoutMs);
      this.pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      const frame: JsonRpcRequest = { jsonrpc: '2.0', id, method };
      if (params !== undefined) frame.params = params;
      this.writeLine(JSON.stringify(frame) + '\n');
    });
  }

  /**
   * Mark the transport dead: reject every pending request. Idempotent; the
   * peer is not usable afterwards (a restarted process builds a new peer).
   */
  close(reason = 'json-rpc transport closed'): void {
    if (this.closed) return;
    this.closed = true;
    const err = new JsonRpcTransportClosedError(reason);
    for (const [, p] of this.pending) {
      if (p.timer) clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  private dispatch(frame: JsonRpcInbound): void {
    const hasMethod = 'method' in frame && typeof frame.method === 'string';
    const hasId = 'id' in frame && frame.id !== undefined;
    if (hasMethod && hasId) {
      void this.answerReverse(frame as JsonRpcRequest);
      return;
    }
    if (hasMethod) {
      this.onNotification(frame.method, (frame as JsonRpcNotification).params);
      return;
    }
    if (hasId) {
      const id = (frame as JsonRpcSuccessResponse | JsonRpcErrorResponse).id;
      const p = this.pending.get(id);
      if (!p) return; // late response for an already-timed-out request
      this.pending.delete(id);
      if (p.timer) clearTimeout(p.timer);
      if ('error' in frame && frame.error) {
        p.reject(
          new JsonRpcRemoteError(
            frame.error.code,
            frame.error.message,
            frame.error.data,
          ),
        );
      } else {
        p.resolve((frame as JsonRpcSuccessResponse).result);
      }
    }
  }

  private async answerReverse(frame: JsonRpcRequest): Promise<void> {
    const respond = (body: object): void => {
      if (!this.closed) {
        this.writeLine(JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...body }) + '\n');
      }
    };
    if (!this.onRequest) {
      respond({
        error: { code: -32601, message: 'Method not found', data: { method: frame.method } },
      });
      return;
    }
    try {
      const result = await this.onRequest(frame.method, frame.params);
      respond({ result: result ?? null });
    } catch (err) {
      respond({
        error: {
          code: -32000,
          message: err instanceof Error ? err.message : String(err),
        },
      });
    }
  }
}
