import type { SidecarHealth, ToolResult } from '@steerable/agent-protocol';

export type SidecarHealthSnapshot = SidecarHealth;
export type SidecarToolResult = ToolResult;

export interface SidecarStartOptions {
  /** Override the python binary; defaults to the bundled portable runtime. */
  pythonExecutable?: string;
  /** Override the entrypoint module; defaults to ``steerable_sidecar``. */
  entryModule?: string;
  /** Extra arguments appended after ``-m <entryModule>``. */
  args?: string[];
  /** Cwd for the spawned process. */
  cwd?: string;
  /** Environment variables to inject. */
  env?: NodeJS.ProcessEnv;
  /** Max ms to wait for the ready handshake before failing. Default 15000. */
  bootTimeoutMs?: number;
  /** ms between health pings. Default 5000. Set <=0 to disable. */
  healthIntervalMs?: number;
  /** consecutive ping failures that trigger an automatic restart. Default 3. */
  restartAfterFailedPings?: number;
  /** Optional hook invoked whenever the sidecar pushes a stream notification. */
  onStreamChunk?: (params: unknown) => void;
  /** Optional hook for log lines emitted on stderr. */
  onLogLine?: (line: string) => void;
}

export interface SidecarMethodOptions {
  /** Per-call timeout in ms. Default 60_000. */
  timeoutMs?: number;
}

/**
 * Wire shape for ``agent.chat.stream`` requests, mirrored from
 * ``packages/sidecar/py/src/steerable_sidecar/sidecar.py``.
 */
export interface SidecarChatStreamRequest {
  provider: string;
  model: string;
  messages: Array<{ role: string; content: string; name?: string; toolCallId?: string }>;
  baseUrl?: string;
  apiKey?: string;
  temperature?: number;
  maxTokens?: number;
  tools?: unknown[];
  streamId?: string;
  providerOptions?: Record<string, unknown>;
  /** Per-start RPC timeout (NOT per-chunk). Default 30_000ms. */
  startTimeoutMs?: number;
}

export interface SidecarStreamChunk {
  streamId: string;
  delta?: string;
  reasoningDelta?: string;
  toolCall?: { id: string; name: string; arguments: Record<string, unknown> };
  finishReason?: string;
  usage?: { promptTokens: number; completionTokens: number; totalTokens: number };
}

export interface SidecarStreamDone {
  streamId: string;
  ok: boolean;
  cancelled?: boolean;
}

export interface SidecarStreamError {
  streamId: string;
  kind: string;
  message: string;
}

export interface SidecarChatStreamHandlers {
  onChunk?: (chunk: SidecarStreamChunk) => void;
  onDone?: (done: SidecarStreamDone) => void;
  onError?: (err: SidecarStreamError) => void;
}
