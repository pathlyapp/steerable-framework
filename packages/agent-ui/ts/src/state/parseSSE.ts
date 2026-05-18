/**
 * `parseSSE` — wire-level Server-Sent-Events parser.
 *
 * Feed it raw text chunks from any source (fetch ReadableStream chunks,
 * Electron IPC `webContents.send` payloads, mocked fixtures, …). It buffers,
 * splits on the `\r?\n\r?\n` frame terminator, and emits one
 * `{ event?, data?, id?, retry? }` per complete frame. JSON parsing of `data`
 * is intentionally out of scope; downstream layers like {@link bridgeLegacySSE}
 * handle that.
 *
 * Replaces deeppath's `SSEParser` and deeppath-agent's `LocalBackendSseParser`
 * with a single dependency-free implementation that compiles to both Node and
 * the browser. Heartbeat-timeout watchdogs (deeppath had a 30 s gate) live on
 * the transport, not here — this class is a pure parser.
 */

export interface SSEFrame {
  /** The `event: …` field; undefined for default `message` events. */
  event?: string;
  /** Concatenated `data: …` lines (newline-joined per the SSE spec). */
  data?: string;
  /** The `id: …` field. */
  id?: string;
  /** The `retry: …` field, as milliseconds. */
  retry?: number;
}

export interface SSEParserOptions {
  onFrame: (frame: SSEFrame) => void;
  onError?: (err: Error) => void;
  /** Called when the stream emits the canonical `data: [DONE]` terminator. */
  onComplete?: () => void;
}

export class SSEParser {
  private buffer = '';
  private completed = false;

  constructor(private readonly options: SSEParserOptions) {}

  /** Feed one chunk. Safe to call with partial UTF-8 strings (we don't decode). */
  feed(chunk: string): void {
    if (this.completed) return;
    this.buffer += chunk;
    const parts = this.buffer.split(/\r?\n\r?\n/);
    this.buffer = parts.pop() ?? '';
    for (const frame of parts) {
      if (frame.length > 0) this.processFrame(frame);
    }
  }

  /** Call when the source signals end-of-stream. Flushes any trailing frame. */
  end(): void {
    if (this.completed) return;
    if (this.buffer.trim().length > 0) this.processFrame(this.buffer);
    this.buffer = '';
  }

  /** Drop all state and stop emitting. */
  cleanup(): void {
    this.buffer = '';
    this.completed = true;
  }

  private processFrame(raw: string): void {
    const frame: SSEFrame = {};
    const dataLines: string[] = [];
    for (const line of raw.split(/\r?\n/)) {
      if (line.length === 0) continue;
      // Comments per the SSE spec — leading `:` lines.
      if (line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.substring(0, colon);
      let value = colon === -1 ? '' : line.substring(colon + 1);
      if (value.startsWith(' ')) value = value.substring(1);
      switch (field) {
        case 'event':
          frame.event = value;
          break;
        case 'data':
          dataLines.push(value);
          break;
        case 'id':
          frame.id = value;
          break;
        case 'retry': {
          const ms = Number.parseInt(value, 10);
          if (Number.isFinite(ms)) frame.retry = ms;
          break;
        }
        default:
          break;
      }
    }
    if (dataLines.length === 0 && frame.event === undefined && frame.id === undefined) {
      return;
    }
    if (dataLines.length > 0) frame.data = dataLines.join('\n');

    if (frame.data === '[DONE]' || frame.data === '"[DONE]"') {
      this.completed = true;
      this.options.onComplete?.();
      return;
    }
    try {
      this.options.onFrame(frame);
    } catch (err) {
      this.options.onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  }
}

/**
 * Tolerant JSON parser for SSE `data:` payloads — returns the raw string when
 * the body isn't JSON, which deeppath relies on for bare-text content deltas.
 */
export function parseSSEData(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}
