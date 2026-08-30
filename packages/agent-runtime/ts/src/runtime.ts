/**
 * `AgentRuntime` — the CoreLoop-level API of the framework for TypeScript
 * products. Every method maps 1:1 onto the sidecar JSON-RPC surface
 * (`docs/spec/sidecar.md`); the runtime drives the same Python CoreLoop the
 * server uses, so there is no second implementation to drift.
 *
 * Streaming is exposed as an async-iterable of `SSEEvent`-shaped frames so
 * callers can `for await` a turn, and as a `done` promise carrying the
 * terminal status. `cancel` / `steer` target the in-flight stream by id.
 */

import type {
  AgentSession,
  HarnessTrace,
  SidecarHealth,
  SSEEvent,
  ToolCall,
  ToolResult,
  TraceEvent,
  TraceSpan,
} from '@steerable/agent-protocol';
import { SidecarProcess, type SidecarProcessOptions } from './sidecar.js';

export interface BranchPoint {
  recordId: string;
  lineage: string;
  seq: number;
  label?: string | null;
}

export interface BranchFamily {
  lineage: string;
  children: unknown[];
}

export interface SessionMessages {
  recordId: string;
  messages: { seq: number; role: string; content: string }[];
}

export interface ToolDescriptor {
  name: string;
  description?: string;
  parameters?: Record<string, unknown>;
}

export interface SkillInfo {
  name: string;
  path: string;
  description?: string;
  [key: string]: unknown;
}

export interface ApplyEditsResult {
  content: string;
  diff: string;
  applied: number;
  matches: unknown[];
}

export interface TraceFetchResult {
  trace: HarnessTrace;
  spans: TraceSpan[];
  events: TraceEvent[];
}

export interface TraceExportResult {
  status: string;
  traceId: string;
  privacyMode: string;
}

/**
 * Wire-level descriptor of one OpenAI-compat flag, as served by
 * `compat.describe`. `kind` is `"bool"`, `"string-list"`, or
 * `"enum:<opt1>,<opt2>"`-style; `default` mirrors the framework's
 * `OpenAICompatFlags` defaults.
 */
export interface CompatFlagDescriptor {
  key: string;
  field: string;
  kind: string;
  default: unknown;
  description: string;
}

export type ChatStreamStatus =
  | 'completed'
  | 'cancelled'
  | 'budget_exhausted'
  | 'error';

export interface ChatStreamHandle {
  /** Server-assigned stream id (also the cancel/steer target). */
  readonly streamId: string;
  /** Turn events: content deltas, tool calls/results, child lifecycle. */
  readonly events: AsyncIterable<SSEEvent>;
  /**
   * Resolves with the terminal status once `stream.done`/`stream.error`
   * arrives. Never rejects for a *completed* turn; rejects only when the
   * transport dies mid-turn.
   */
  readonly done: Promise<{ status: ChatStreamStatus; cancelled: boolean }>;
}

export interface ChatStreamParams {
  /** LLM provider kind: "openai_compat" | "anthropic" | <custom>. */
  provider: string;
  /** Model id understood by the provider. */
  model: string;
  /**
   * Full message list for the turn (OpenAI chat shape). This doubles as the
   * WS3 history seed: the sidecar reconciles it against the session record.
   */
  messages: Array<{ role: string; content: unknown; [key: string]: unknown }>;
  baseUrl?: string;
  apiKey?: string;
  /** History seed window the host already rendered (see WS3 contract). */
  historySeed?: unknown[];
  /** Orchestration opt-in: {maxDepth?, maxParallel?, childMaxRounds?}. */
  orchestration?: {
    maxDepth?: number;
    maxParallel?: number;
    childMaxRounds?: number;
  };
  /** Per-request tool domain narrowing (fail-closed on the loop side). */
  toolFilter?: string[];
  /** Approval policy for this turn. */
  approval?: Record<string, unknown>;
  /** Anything else the sidecar accepts is passed through untouched. */
  [key: string]: unknown;
}

export interface AgentRuntimeOptions extends SidecarProcessOptions {
  /**
   * Host tool handlers for the reverse channel: the CoreLoop calls these
   * via `tool.invoke` while a turn runs. Register at construction or with
   * `registerTool`.
   */
  tools?: Record<
    string,
    (args: Record<string, unknown>) => Promise<Partial<ToolResult> | string>
  >;
}

interface StreamChunkParams {
  streamId: string;
  delta?: string;
  toolCall?: ToolCall;
  toolResult?: ToolResult;
  usage?: unknown;
  finishReason?: string;
}

interface StreamDoneParams {
  streamId: string;
  ok: boolean;
  cancelled?: boolean;
  status?: string;
}

interface StreamErrorParams {
  streamId: string;
  kind: string;
  message: string;
}

interface StreamListener {
  push: (event: SSEEvent) => void;
  finish: () => void;
  fail: (err: Error) => void;
}

/**
 * Sink for one in-flight stream. Events are single-consumer: they buffer
 * until the first `events` iterable attaches, then flow live. `whenDone`
 * settles on the terminal status regardless of whether anyone consumed the
 * events, and a consumer that attaches after termination drains the buffer
 * and completes immediately.
 */
class StreamSink {
  readonly whenDone: Promise<{ status: ChatStreamStatus; cancelled: boolean }>;
  private buffer: SSEEvent[] = [];
  private listener: StreamListener | null = null;
  private settle!: {
    resolve: (v: { status: ChatStreamStatus; cancelled: boolean }) => void;
    reject: (err: Error) => void;
  };
  private terminal: { status: ChatStreamStatus; cancelled: boolean } | null =
    null;
  private failure: Error | null = null;
  /**
   * Set by `chatStream` once it has captured this sink from the map. The
   * map entry may be deleted on termination only after the claim — a done
   * frame that coalesces with the response into one read must not orphan
   * the events before the caller gets them.
   */
  claimed = false;

  get isTerminal(): boolean {
    return this.terminal !== null;
  }

  constructor() {
    this.whenDone = new Promise((resolve, reject) => {
      this.settle = { resolve, reject };
    });
    // A stream nobody awaits must not crash the host on transport failure.
    this.whenDone.catch(() => undefined);
  }

  push(event: SSEEvent): void {
    if (this.terminal) return;
    if (this.listener) this.listener.push(event);
    else this.buffer.push(event);
  }

  finish(status: ChatStreamStatus, cancelled: boolean): void {
    if (this.terminal) return;
    this.terminal = { status, cancelled };
    this.listener?.finish();
    this.settle.resolve(this.terminal);
  }

  fail(err: Error): void {
    if (this.terminal) return;
    this.terminal = { status: 'error', cancelled: false };
    this.failure = err;
    this.listener?.fail(err);
    this.settle.reject(err);
  }

  /** Single-consumer attach: replay the buffer, then flow live. */
  attach(listener: StreamListener): void {
    if (this.listener) {
      throw new Error('stream events are single-consumer and already attached');
    }
    const buffered = this.buffer;
    this.buffer = [];
    if (this.failure) {
      listener.fail(this.failure);
      return;
    }
    this.listener = listener;
    for (const event of buffered) listener.push(event);
    if (this.terminal) listener.finish();
  }

  detach(listener: StreamListener): void {
    if (this.listener === listener) this.listener = null;
  }
}

/**
 * Host confined-spawn capability (W2.2.1): the sidecar sends
 * `host.process.spawn` when the platform has no command-rewriting sandbox
 * backend (Windows) and the request opted into host spawn. The host spawns
 * the command confined (restricted token + JobObject on Windows) and reports
 * the enforcement it actually applied. Contract: docs/spec/safety.md
 * "Host capability surface".
 */
export interface HostSpawnRequest {
  command: string;
  cwd?: string;
  policy: {
    writableRoots: string[];
    network: boolean;
    allowedHosts: string[];
  };
  context?: { chatId?: string };
}

export interface HostSpawnResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  truncated?: boolean;
  /** Enforcement the host actually applied; omitting it reports `none`. */
  sandbox?: { backend: string; enforcement: 'full' | 'partial' | 'none' };
}

export class AgentRuntime {
  readonly process: SidecarProcess;
  private readonly toolHandlers = new Map<
    string,
    (args: Record<string, unknown>) => Promise<Partial<ToolResult> | string>
  >();
  private spawnHandler:
    | ((request: HostSpawnRequest) => Promise<HostSpawnResult>)
    | null = null;
  private readonly streams = new Map<string, StreamSink>();

  constructor(options: AgentRuntimeOptions = {}) {
    this.process = new SidecarProcess({
      ...options,
      onRequest: (method, params) => this.handleReverse(method, params),
      onNotification: (method, params) => {
        this.routeStreamNotification(method, params);
        options.onNotification?.(method, params);
      },
    });
    for (const [name, handler] of Object.entries(options.tools ?? {})) {
      this.toolHandlers.set(name, handler);
    }
  }

  /** Spawn the sidecar and wait for readiness. */
  start(): Promise<void> {
    return this.process.start();
  }

  /** Graceful drain and exit; in-flight streams finish or fail first. */
  async close(): Promise<void> {
    await this.process.close();
    for (const [, s] of this.streams) {
      s.fail(new Error('runtime closed mid-stream'));
    }
    this.streams.clear();
  }

  /** Register a host tool the CoreLoop may call back during turns. */
  registerTool(
    name: string,
    handler: (args: Record<string, unknown>) => Promise<Partial<ToolResult> | string>,
  ): void {
    this.toolHandlers.set(name, handler);
  }

  /**
   * Register the host confined-spawn capability. Without a handler,
   * `host.process.spawn` reverse calls are rejected and the sidecar fails
   * closed (the command never runs unsandboxed on no-backend platforms).
   */
  onProcessSpawn(
    handler: (request: HostSpawnRequest) => Promise<HostSpawnResult>,
  ): void {
    this.spawnHandler = handler;
  }

  // ---- system ----------------------------------------------------------

  ping(): Promise<SidecarHealth> {
    return this.process.request('system.ping');
  }

  // ---- sessions --------------------------------------------------------

  createSession(input: {
    chatId: string;
    userId: string;
    projectId?: string | null;
    scenario?: string;
    stageData?: Record<string, unknown> | null;
  }): Promise<AgentSession> {
    return this.process.request('agent.session.create', input);
  }

  resumeSession(sessionId: string): Promise<AgentSession> {
    return this.process.request('agent.session.resume', { sessionId });
  }

  listSessions(filter: {
    userId?: string;
    chatId?: string;
    activeOnly?: boolean;
  } = {}): Promise<AgentSession[]> {
    return this.process.request('agent.session.list', filter);
  }

  /** Fork a record into a new branch without running a turn. */
  forkSession(params: {
    sessionId: string;
    recordId: string;
    label?: string;
  }): Promise<BranchPoint> {
    return this.process.request('agent.session.fork', params);
  }

  /** Branch-family view for a lineage. */
  sessionBranches(lineage: string): Promise<BranchFamily> {
    return this.process.request('agent.session.branches', { lineage });
  }

  /**
   * Projected post-boundary message span of a history record — what the
   * model would see on resume. Fails loud on unknown records.
   */
  sessionMessages(recordId: string): Promise<SessionMessages> {
    return this.process.request('agent.session.messages', { recordId });
  }

  // ---- chat ------------------------------------------------------------

  /**
   * Start one CoreLoop turn. Resolves with a handle as soon as the sidecar
   * assigns a stream id; consume `handle.events` for the turn and await
   * `handle.done` for the terminal status.
   */
  async chatStream(params: ChatStreamParams): Promise<ChatStreamHandle> {
    // The sidecar's CoreLoop path is opt-in per request; this runtime's
    // whole API (cooperative cancel, steer, orchestration, bounded
    // fragments) only exists there, so default it on. A caller embedding
    // against the legacy direct-stream path can still pass
    // `useCoreLoop: false` explicitly.
    const { streamId } = await this.process.request<{ streamId: string }>(
      'agent.chat.stream',
      { useCoreLoop: true, ...params },
    );
    // Chunks can already have arrived (the sidecar starts streaming before
    // the response reaches us) — routeStreamNotification registers the sink
    // eagerly in that case; otherwise create it now. Claiming keeps the map
    // entry alive past termination until this lookup has happened.
    let sink = this.streams.get(streamId);
    if (!sink) {
      sink = new StreamSink();
      this.streams.set(streamId, sink);
    }
    sink.claimed = true;
    if (sink.isTerminal) this.streams.delete(streamId);
    return { streamId, events: streamIterable(sink), done: sink.whenDone };
  }

  /** Cooperative cancel: the loop winds down at the next safe point. */
  async cancelChat(streamId: string): Promise<void> {
    await this.process.request('agent.chat.cancel', { streamId });
  }

  /** Mid-turn steer; resolves true when the running turn accepted it. */
  async steerChat(streamId: string, content: string): Promise<boolean> {
    const res = await this.process.request<{ accepted?: boolean }>(
      'agent.chat.steer',
      { streamId, content },
    );
    return res?.accepted === true;
  }

  /** Fork the record of a running turn. */
  forkChat(streamId: string, label?: string): Promise<BranchPoint> {
    return this.process.request('agent.chat.fork', { streamId, label });
  }

  // ---- tools / skills / workspace --------------------------------------

  listTools(): Promise<ToolDescriptor[]> {
    return this.process.request('tool.list');
  }

  invokeTool(call: ToolCall): Promise<ToolResult> {
    return this.process.request('tool.invoke', call);
  }

  listSkills(roots: string[]): Promise<{ skills: SkillInfo[] }> {
    return this.process.request('skills.list', { roots });
  }

  applyEdits(params: {
    content: string;
    edits: unknown[];
  }): Promise<ApplyEditsResult> {
    return this.process.request('workspace.apply_edits', params);
  }

  // ---- trace / config ----------------------------------------------------

  fetchTrace(traceId: string): Promise<TraceFetchResult> {
    return this.process.request('trace.fetch', { traceId });
  }

  exportTrace(traceId: string): Promise<TraceExportResult> {
    return this.process.request('trace.export', { traceId });
  }

  getConfig(): Promise<Record<string, unknown>> {
    return this.process.request('config.get');
  }

  async setConfig(patch: Record<string, unknown>): Promise<void> {
    await this.process.request('config.set', patch);
  }

  /**
   * The framework-owned OpenAI-compat flag vocabulary (`describe_compat_flags`
   * on the Python side). Host settings UIs render their compat section from
   * these descriptors instead of hardcoding flag names, so a new flag needs
   * no host-side constant to stay in sync.
   */
  describeCompatFlags(): Promise<{ flags: CompatFlagDescriptor[] }> {
    return this.process.request('compat.describe');
  }

  // ---- internals ---------------------------------------------------------

  private handleReverse(method: string, params: unknown): Promise<unknown> {
    if (method === 'tool.invoke') {
      const call = params as ToolCall | undefined;
      const handler = call && this.toolHandlers.get(call.name);
      if (!call || !handler) {
        return Promise.reject(
          new Error(`tool.invoke: no host handler for ${call?.name ?? '<missing>'}`),
        );
      }
      return handler((call.arguments ?? {}) as Record<string, unknown>);
    }
    if (method === 'host.process.spawn') {
      if (!this.spawnHandler) {
        return Promise.reject(
          new Error('host.process.spawn: capability not implemented by this host'),
        );
      }
      return this.spawnHandler(params as HostSpawnRequest);
    }
    return Promise.reject(new Error(`unsupported reverse method ${method}`));
  }

  private routeStreamNotification(method: string, params: unknown): void {
    const p = (params ?? {}) as { streamId?: string };
    if (!p.streamId) return;
    if (method === 'stream.chunk' && !this.streams.has(p.streamId)) {
      // Chunks can arrive before chatStream's response resolves (the sidecar
      // starts streaming immediately) — register the sink eagerly.
      this.streams.set(p.streamId, new StreamSink());
    }
    const state = this.streams.get(p.streamId);
    if (!state) return;
    switch (method) {
      case 'stream.chunk': {
        const c = params as StreamChunkParams;
        if (c.delta) {
          state.push({ type: 'content', content: c.delta } as SSEEvent);
        }
        if (c.toolCall) {
          state.push({ type: 'tool_call', payload: c.toolCall } as unknown as SSEEvent);
        }
        if (c.toolResult) {
          state.push({ type: 'tool_result', payload: c.toolResult } as unknown as SSEEvent);
        }
        if (c.usage) {
          state.push({ type: 'usage', payload: c.usage } as unknown as SSEEvent);
        }
        return;
      }
      case 'stream.done': {
        const d = params as StreamDoneParams;
        state.push({ type: 'done' } as SSEEvent);
        const status = (d.status ??
          (d.cancelled ? 'cancelled' : d.ok ? 'completed' : 'error')) as ChatStreamStatus;
        state.finish(status, d.cancelled === true || status === 'cancelled');
        if (state.claimed) this.streams.delete(p.streamId);
        return;
      }
      case 'stream.error': {
        const e = params as StreamErrorParams;
        state.push({ type: 'error', message: e.message } as SSEEvent);
        state.finish('error', false);
        if (state.claimed) this.streams.delete(p.streamId);
        return;
      }
      case 'agent.child': {
        state.push({
          type: 'orchestration',
          payload: params,
        } as unknown as SSEEvent);
        return;
      }
      default:
        return;
    }
  }
}

function streamIterable(sink: StreamSink): AsyncIterable<SSEEvent> {
  return {
    [Symbol.asyncIterator]() {
      const queue: SSEEvent[] = [];
      let done = false;
      let error: Error | null = null;
      let wake: (() => void) | null = null;
      const listener: StreamListener = {
        push(event: SSEEvent) {
          queue.push(event);
          wake?.();
        },
        finish() {
          done = true;
          wake?.();
        },
        fail(err: Error) {
          error = err;
          wake?.();
        },
      };
      sink.attach(listener);
      return {
        async next(): Promise<IteratorResult<SSEEvent>> {
          for (;;) {
            const event = queue.shift();
            if (event !== undefined) return { value: event, done: false };
            if (error) throw error;
            if (done) return { value: undefined, done: true };
            await new Promise<void>((resolve) => {
              wake = resolve;
            });
            wake = null;
          }
        },
        async return(): Promise<IteratorResult<SSEEvent>> {
          sink.detach(listener);
          return { value: undefined, done: true };
        },
      };
    },
  };
}
