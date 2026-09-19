import type { SidecarHealth, ToolResult } from '@steerable/agent-protocol';

export type SidecarHealthSnapshot = SidecarHealth;
export type SidecarToolResult = ToolResult;

/**
 * W4-3: layer-1 (sidecar process sandbox) posture, recorded by the
 * supervisor at spawn-plan time. `enforcement` mirrors the layer-3
 * `_sandbox.enforcement` vocabulary (`full | partial | none`) so both
 * layers read consistently; layer 1 never reports `full` because Seatbelt
 * egress on remote hosts is port-only (docs/spec/safety.md). `reason`
 * distinguishes an explicit opt-out (`disabled_by_option` /
 * `disabled_by_env`) from a refused start
 * (`platform_unsupported` / `seatbelt_missing` / `profile_failed` /
 * `wrap_failed` / `helper_missing`) — confinement requested means the
 * process does not run unsandboxed. The settings UI warns on the latter.
 */
export interface SidecarSandboxPosture {
  backend: 'seatbelt' | 'bwrap' | 'landlock' | 'windows-restricted-token' | 'none';
  enforcement: 'partial' | 'none';
  reason:
    | 'active'
    | 'disabled_by_option'
    | 'disabled_by_env'
    | 'platform_unsupported'
    | 'seatbelt_missing'
    | 'profile_failed'
    | 'wrap_failed'
    | 'helper_missing';
}

export interface SidecarStartOptions {
  /** Override the python binary; defaults to the bundled portable runtime. */
  pythonExecutable?: string;
  /** Optional Rust sidecar binary; used when STEERABLE_RUST_SIDECAR is on. */
  rustSidecarBin?: string;
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
  /**
   * Confine the sidecar in an OS sandbox. macOS: Seatbelt. Linux: bwrap
   * then Landlock wrapping the python process. Windows: win-spawn-helper
   * `--passthrough` (restricted token + Job Object) with inherited stdio.
   * If confinement cannot be applied, start() refuses rather than spawning
   * unsandboxed. `false` or `STEERABLE_SIDECAR_SANDBOX=0` is the only
   * unsandboxed path. Default ON.
   */
  sandbox?: boolean;
  /**
   * Egress allow-list for the sandboxed sidecar (entries `host` or
   * `host:port`; bare hosts allow 443+80). Once any entry is given the
   * profile fails closed: outbound is denied except to the declared
   * endpoints. Unset/empty keeps outbound fully open (the default).
   * Seatbelt cannot match hostnames — localhost entries pin
   * `localhost:PORT` exactly, remote entries degrade to their port.
   * Only consulted when `sandbox` is on. Defaults to the
   * `STEERABLE_SIDECAR_SANDBOX_ALLOWED_HOSTS` env (comma-separated).
   */
  sandboxAllowedHosts?: string[];
  /**
   * Also allow what the network-read tools (`web_fetch`/`web_search`) need on
   * top of the allow-list: the system resolver plus outbound http(s) to any
   * host. Their targets are whatever the model asks for, so no host list can
   * name them ahead of time — without this every fetch fails name resolution
   * inside the SSRF pre-check. Set it only where those tools are offered; it
   * grants reach to any host on ports 80 and 443. No effect when the
   * allow-list is unset (outbound already open) or `sandbox` is off.
   */
  sandboxWebEgress?: boolean;
  /**
   * Allow name resolution (the system resolver socket — no IP reach) on top
   * of a fail-closed egress allow-list. Set when egress is pinned to the
   * per-host proxy and the web tools stay offered: `web_fetch`'s SSRF
   * pre-check resolves locally even though the fetch itself tunnels through
   * the proxy. No effect when the allow-list is unset (outbound already
   * open) or `sandboxWebEgress` is on (its profile already grants the
   * resolver). macOS Seatbelt only.
   */
  sandboxAllowResolver?: boolean;
  /**
   * Extra directories the confined sidecar may write to, beyond the default
   * `~/.steerable` root. Each entry is forwarded to the platform sandbox as
   * an additional writable root (macOS Seatbelt `--writable-root`, Linux
   * bwrap/Landlock `--writable-root`, Windows win-spawn-helper
   * `--writable-root`). Needed whenever `--storage-path` (or any other
   * sidecar-written file) lives outside `~/.steerable` — e.g. tests keeping
   * sessions.db in a temp dir, or BS mode with `DEEPPATH_USER_DATA_DIR`.
   */
  sandboxWritableRoots?: string[];
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
 * A request the sidecar sends *to* the host over the reverse channel
 * (see spec/sidecar/README.md "Reverse channel"). Distinguished from a
 * response by the presence of both `id` and `method`.
 */
export interface SidecarReverseRequest {
  /** Reverse-call id; sidecar uses `srv_`-prefixed strings. */
  id: string;
  method: string;
  params?: unknown;
}

/**
 * Host-side handler for a reverse request. Receives the params and returns
 * the `result` payload to send back, or throws to return an error.
 */
export type SidecarReverseHandler = (params: unknown) => Promise<unknown> | unknown;

/** W5-2: `agent.session.fork` result — the created branch point. */
export interface SidecarSessionForkResult {
  recordId: string;
  sourceRecordId: string | null;
  sourceUntilSeq: number | null;
  label: string;
  seedMessages: number;
}

/**
 * Result of an `agent.session.fork` attempt.
 *
 * `declined` means the sidecar answered and rejected the address — no record, or
 * a fork point it will not split, such as an ordinal inside a branch seed, where
 * the protocol documents host fallback as the expected path. Anything else is a
 * fork that was supposed to happen and did not (transport, timeout, fault), and
 * the caller must not treat the two alike: only the first is a path the
 * framework sanctions.
 */
export type SidecarSessionForkOutcome =
  | { ok: true; fork: SidecarSessionForkResult }
  | { ok: false; declined: boolean; reason: string };

/** W1.2.1: one node in an `agent.session.branches` lineage/children list. */
export interface SidecarBranchPoint {
  recordId: string;
  sourceRecordId: string | null;
  sourceUntilSeq: number | null;
  label: string;
  depth?: number;
}

/** W1.2.1: `agent.session.branches` result. */
export interface SidecarSessionBranches {
  lineage: SidecarBranchPoint[];
  children: SidecarBranchPoint[];
}

/**
 * Session tree: one node in an `agent.session.tree` family tree
 * (recursive). Same fields as {@link SidecarBranchPoint} plus nested
 * `children`; `depth` is 0 on the family root and matches lineage
 * numbering along the chain.
 */
export interface SidecarSessionTreeNode {
  recordId: string;
  sourceRecordId: string | null;
  sourceUntilSeq: number | null;
  label: string;
  depth: number;
  children: SidecarSessionTreeNode[];
}

/**
 * Session tree: `agent.session.tree` result — the full branch family
 * containing the queried record, expanded from the family root.
 * `truncated` means the sidecar's safety bounds (depth 32 / 500 nodes)
 * cut the expansion; the tree is still valid below the cut.
 */
export interface SidecarSessionTree {
  recordId: string;
  tree: SidecarSessionTreeNode;
  nodeCount: number;
  truncated: boolean;
}

/** W1.2.1: `agent.session.messages` result — the projected visible span. */
export interface SidecarSessionMessages {
  recordId: string;
  messages: Array<{ seq: number; role: string; content: string }>;
}

/**
 * W6-1: `workspace.apply_edits` result — the pure structured-edit algorithm
 * run in the sidecar on host-supplied content. The host owns all file I/O.
 */
export interface SidecarApplyEditsResult {
  content: string;
  diff: string;
  applied: number;
  matches: Array<{
    level: 'exact' | 'trim' | 'unicode';
    startLine: number;
    oldLineCount: number;
  }>;
}

/**
 * W6-7/skills: `skills.list` result item — a parsed SKILL.md module, mirroring
 * the desktop's `SkillModule`. Parsing is single-sourced in the framework's
 * `skills.py`; the desktop no longer re-parses frontmatter.
 */
export interface SidecarSkillModule {
  name: string;
  displayName: string;
  description: string;
  priority: number;
  tags: string[];
  conditions: string[];
  match: 'any' | 'all';
  layer: 'eager' | 'catalog';
  modelInvocable: boolean;
  content: string;
  dirName: string;
  skillsDir: string;
}

/**
 * Wire shape for ``agent.chat.stream`` requests, mirrored from
 * ``packages/sidecar/py/src/steerable_sidecar/sidecar.py``.
 */
export interface SidecarChatStreamRequest {
  provider: string;
  model: string;
  messages: Array<{
    role: string;
    content: string;
    name?: string;
    toolCallId?: string;
    /**
     * Assistant 消息的工具调用回传：缺失时下一轮里的 `role:'tool'` 消息会
     * 被 OpenAI 严格协议判为孤儿（400 "must be a response to a preceding
     * message with 'tool_calls'"）。与 in-process openai-compat.ts 的
     * mapMessage 同一约束。
     */
    toolCalls?: Array<{ id: string; name: string; arguments: Record<string, unknown> }>;
    /**
     * Thinking 模型的推理回传（DeepSeek thinking 模式强制要求，缺了第二轮
     * 400）。sidecar 侧按 compat.reasoningEchoField 落到正确字段名。
     */
    reasoningContent?: string;
    /**
     * W6-3: structured content parts (multimodal). When present the sidecar
     * treats it as authoritative over `content` (which then is just the text
     * projection). Mirrors `spec/chat/ContentPart.schema.json`.
     */
    parts?: Array<
      | { type: 'text'; text: string }
      | { type: 'image'; data?: string; url?: string; mediaType?: string }
    >;
  }>;
  baseUrl?: string;
  apiKey?: string;
  temperature?: number;
  /** W2.8.2: the host-assembled system prompt as a typed fragment (the
   * sidecar enforces its token cap at the seed boundary). Mutually
   * exclusive with a leading `system` message in `messages` — the sidecar
   * rejects both-present as a host bug. */
  systemPrompt?: string;
  /** W1.3.2 explicit OpenAI-compat flag overrides (camelCase wire keys owned
   * by the framework's `OpenAICompatFlags.from_dict`; unknown keys fail loud
   * there). Omitted → the sidecar auto-detects from the base-URL host. */
  compat?: Record<string, unknown>;
  /** Provider-preset choice (framework `llm.presets`): omitted/{"enabled":
   * true} → auto-match on base-URL+model; {"enabled": false} → layer off;
   * {"override": {...}} → pinned preset (camelCase keys owned by the
   * framework's `ProviderPreset.from_dict`; unknown keys fail loud there). */
  presets?: Record<string, unknown>;
  maxTokens?: number;
  /**
   * Per-turn reasoning effort from the host's model picker. The sidecar
   * validates it strict against the resolved catalog entry (a level the
   * model cannot honor is an `invalid_params` RPC error at stream start,
   * never a silently dropped field); omitted → env/preset defaults apply.
   */
  reasoningEffort?: string;
  tools?: unknown[];
  streamId?: string;
  providerOptions?: Record<string, unknown>;
  /** Per-start RPC timeout (NOT per-chunk). Default 30_000ms. */
  startTimeoutMs?: number;
  /** Route the stream through the sidecar-hosted Python CoreLoop (A4). */
  useCoreLoop?: boolean;
  /**
   * W7-1: continue the durable record's interrupted turn instead of opening
   * a new one. The sidecar substitutes the record's projected transcript
   * (dangling tool_calls closed) as the loop seed; `messages` must be empty
   * and `recordId` (or `chatId`) must name a non-empty record. CoreLoop-only.
   */
  resume?: boolean;
  /**
   * Select which CoreLoop assistant text reaches the host. `all` streams
   * every tool-round narration; `final` emits only the terminal tool-free
   * response while preserving intermediate rounds in the record and trace.
   */
  contentMode?: 'all' | 'final';
  /**
   * Forward every pre-digestion provider chunk (the loop's `on_stream_chunk`
   * hook, before UI-tag stripping) as `stream.chunk` notifications carrying
   * `rawChunk` — for hosts running incremental renderers. Default off: it
   * costs one notification per chunk. CoreLoop-only.
   */
  streamRawChunks?: boolean;
  /** Execute every tool call on the host via the reverse channel. */
  toolsViaHost?: boolean;
  /**
   * W8: advertise the sidecar-hosted `ask_user` tool (structured user
   * questions). The host answers `ask_user.request` reverse calls with the
   * renderer's question card; under `toolsViaHost` the sidecar intercepts
   * `ask_user` locally so it never reaches the host's tool.invoke.
   */
  askUser?: boolean;
  /** Embedder context forwarded on every reverse tool.invoke (e.g. {mode}). */
  toolContext?: Record<string, unknown>;
  /** Enable the anti-hallucination hook layer (routing / deferred-claimed
   * retry / grounding judge / narration). `true` or an options object. */
  antiHallucination?: boolean | { maxRetries?: number };
  /** P3.1 multi-agent orchestration: OPT-IN advanced mode (off by default
   * since the delegate-on-pool unification). Only `enabled: true` wraps the
   * executor with the six-tool orchestration family (agent_spawn /
   * agent_send / agent_wait / agent_close / agent_list / agent_interrupt).
   * When delegation is also on (it is by default), both surfaces share one
   * agent pool — one maxParallel budget, one lineage space. */
  orchestration?: {
    enabled?: boolean;
    maxDepth?: number;
    maxParallel?: number;
    childMaxRounds?: number;
  };
  /**
   * Sub-agent delegation (delegate-on-pool). ON BY DEFAULT on the sidecar —
   * pass `false` to disable, or an options object to configure. The model
   * gets the single `delegate_subagent` tool; children run as pooled
   * AgentPool runs emitting `agent.child` lifecycle notifications (the
   * spawn payload carries the resolved `profile` name). `profiles` adds
   * named subagent_type profiles (CC parity) with per-profile tool domains,
   * models, round bounds, and concurrency.
   */
  subagent?: boolean | {
    toolFilter?: string[];
    maxParallel?: number;
    profiles?: Record<string, {
      toolFilter?: string[];
      model?: string;
      maxRounds?: number;
      concurrent?: boolean;
      description?: string;
      /** Profile system prompt, seeded as the child loop's first message
       * (CC `.claude/agents` body parity). */
      systemPrompt?: string;
    }>;
  };
  /** A6 layered skill disclosure: the sidecar injects the catalog layer
   * (first-round pre_step, recorded as a hook_action event) and answers
   * `skill` tool calls with full bodies; the eager layer stays in the
   * host-built system prompt. `mode: 'eager'` keeps everything host-side. */
  skills?: {
    roots: string[];
    conditions?: string[];
    exclude?: string[];
    ignoreConditions?: boolean;
    mode?: 'layered' | 'eager';
  };
  chatId?: string;
  /** W5-2: the durable record this turn appends to. Defaults to `chatId`
   * on the sidecar; after a regenerate-fork the chat's active record is the
   * branch id, so the host must pass it explicitly. */
  recordId?: string;
  /** Wave 2 world-state sections: slow-changing host context (current
   * time, timezone, …) as plain per-section JSON data. The sidecar injects
   * it once as a `<world-state>` fragment; later turns diff against the
   * snapshot embedded in the record — unchanged state costs zero tokens, a
   * change costs one small RFC 7386 tail patch. */
  worldState?: Record<string, unknown>;
  /** CoreLoop tunables, mapped to LoopConfig on the sidecar. */
  maxRounds?: number;
  maxToolErrors?: number;
  budgetTokens?: number;
  softTimeoutMs?: number;
  /** Per-tool-execution timeout (ms). On expiry the call returns a failed
   * ToolResult (error `tool_timeout`) instead of hanging the turn. */
  toolTimeoutMs?: number;
    /**
     * Wave 4 (W4-2): per-exec OS sandbox for shell/subprocess tool calls
     * (`SandboxedToolExecutor` on the sidecar). The command is rewritten into
     * a Seatbelt invocation before it crosses the reverse channel, so the
     * host's shell spawns the confined command without learning sandbox
     * mechanics. Every sandboxed result carries `data._sandbox =
     * {backend, enforcement: full|partial|none}`. `requireFull: true`
     * denies anything weaker than `full` (including honest `partial`).
     * `requireBackend: true` denies only `none` (no OS backend). Absent →
     * unconfined (legacy behavior).
     */
  execSandbox?: {
    enabled: boolean;
    /** Directories the confined command may write into (e.g. project root). */
    writableRoots?: string[];
    /** Allow outbound network from the confined command. Default false. */
    network?: boolean;
    /** Egress allow-list (host[:port]); only consulted when network is on. */
    allowedHosts?: string[];
    /** Deny the call unless enforcement is `full`. Default false (marked). */
    requireFull?: boolean;
    /**
     * Deny the call when there is no sandbox backend (`enforcement: none`).
     * Partial backends (Seatbelt with `network: true`) still run. Default false.
     */
    requireBackend?: boolean;
    /**
     * W4.1.1: when no local rewriter backend exists (Windows), delegate
     * confined spawn to the host over the `host.process.spawn` reverse
     * channel instead of running unconfined. The host reports the
     * enforcement it actually applied; a host without the capability fails
     * closed (tool error, the command never runs).
     */
    hostSpawn?: boolean;
  };
  /**
   * Wave 4 (W4-1): approval algebra in front of every tool call.
   * `mode: 'host'` asks the host UI over the reverse channel
   * (`approval.request`); `storePath` enables the durable scope
   * (`allow_always` / `deny_always` persisted per category);
   * `timeoutMs` fails closed as `timed_out` (a denial) when the UI does
   * not answer in time. Absent → no approval layer (legacy behavior).
   */
  approval?: {
    mode: 'host' | 'auto';
    timeoutMs?: number;
    storePath?: string;
  };
}

/**
 * Pre-digestion raw provider chunk, forwarded by the sidecar when the
 * request sets `streamRawChunks: true` (default off). The CoreLoop's
 * `on_stream_chunk` hook observes every `LLMStreamChunk` *before* UI-tag
 * stripping and surrogate splitting turn it into display text — this is the
 * input for incremental renderers (e.g. a streaming UI-tag parser). The
 * digested `delta` / `reasoningDelta` fields on the same notification stay
 * post-stripping display text. Unset fields are omitted; the provider's
 * original wire chunk (`raw`) and per-chunk `usage` are never forwarded.
 *
 * Note: the OpenAI-compat provider buffers tool-call argument fragments
 * into one complete ToolCall, so `toolCallDelta` arrives whole — only
 * content/reasoning are incremental.
 */
export interface SidecarRawChunk {
  contentDelta?: string;
  reasoningDelta?: string;
  toolCallDelta?: { id: string; name: string; arguments: Record<string, unknown> };
  finishReason?: string;
}

export interface SidecarStreamChunk {
  streamId: string;
  delta?: string;
  reasoningDelta?: string;
  toolCall?: { id: string; name: string; arguments: Record<string, unknown> };
  /**
   * Pre-digestion chunk (opt-in via `streamRawChunks`). Fire-and-forget
   * emission on the sidecar: chunk order is preserved, ordering against the
   * digested fields on sibling notifications is not.
   */
  rawChunk?: SidecarRawChunk;
  /** CoreLoop tool progress (A4 path). */
  toolResult?: {
    id: string;
    name: string;
    success: boolean;
    durationMs?: number;
    error?: string;
    resultPreview?: string;
    /** W4-2: `data._sandbox` marker lifted out of the result for the card. */
    sandbox?: { backend?: string; enforcement: string };
  };
  /** CoreLoop notices: soft_timeout / budget_exhausted / round_end / hook_action. */
  notice?: { kind: string; [key: string]: unknown };
  finishReason?: string;
  usage?: { promptTokens: number; completionTokens: number; totalTokens: number };
}

export interface SidecarStreamDone {
  streamId: string;
  ok: boolean;
  cancelled?: boolean;
  /** CoreLoop terminal status (completed | failed | budget_exhausted). */
  status?: string;
  reason?: string;
  /** Sidecar TraceRecorder id — fetch the persisted run via `trace.fetch`. */
  traceId?: string;
  /**
   * W6-9: the run's accumulated billable usage (summed over every provider
   * request this turn), plus `costUsd` when the model is priced. Absent on
   * paths that don't report usage.
   */
  usage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    cachedPromptTokens?: number;
    costUsd?: number;
  };
}

export interface SidecarStreamError {
  streamId: string;
  /** Present on CoreLoop failures — the sidecar recorded the partial trace. */
  traceId?: string;
  kind: string;
  message: string;
}

/**
 * `agent.child` notification payload (child-agent lifecycle), demuxed by
 * streamId. Emitted by the orchestration pool and by `delegate_subagent`
 * delegations (delegate-on-pool). `kind` is one of child_spawned /
 * child_completed / child_failed / child_cancelled / child_interrupted /
 * child_resumed; the rest of the fields depend on the kind (childId always
 * present; child_spawned adds `task` and `depth`; delegations add
 * `profile` — the resolved subagent_type, `general-purpose` when untyped).
 */
export interface SidecarChildEvent {
  kind: string;
  childId: string;
  task?: string;
  depth?: number;
  status?: string;
  error?: string;
  profile?: string;
}

export interface SidecarChatStreamHandlers {
  onChunk?: (chunk: SidecarStreamChunk) => void;
  onDone?: (done: SidecarStreamDone) => void;
  onError?: (err: SidecarStreamError) => void;
  onChildEvent?: (event: SidecarChildEvent) => void;
}

/**
 * `models.list` result row: one model id the configured gateway accepts,
 * joined with the bundled models.dev capability catalog by the same
 * resolution the request path's reasoning-effort clamp uses. `joinedFrom`
 * keeps the leaf-join provenance; `capabilities: 'unknown'` means no
 * catalog tier matched — reasoning levels are then unavailable rather
 * than empty.
 */
export interface SidecarModelEntry {
  id: string;
  name: string | null;
  window: number | null;
  modalities: string[];
  reasoningLevels: string[];
  pricing: {
    promptPerMtok: number | null;
    completionPerMtok: number | null;
  } | null;
  joinedFrom: string | null;
  capabilities: 'known' | 'unknown';
}

/**
 * `models.list` result. `catalogStatus` is `live` when just fetched from
 * the gateway, `stale` when served from cache after a refresh failure,
 * `offline` when the gateway is unreachable with no cache (then `models`
 * is empty and `error` carries the cause). Discovery only — the catalog
 * is not a routing whitelist; unlisted ids may still be sent.
 */
export interface SidecarModelCatalog {
  models: SidecarModelEntry[];
  catalogStatus: 'live' | 'stale' | 'offline';
  error?: string;
  fetchedAt?: number;
  /** Absent on the offline path (no gateway/env context to report). */
  current?: { model: string | null; reasoningEffort: string | null };
}
