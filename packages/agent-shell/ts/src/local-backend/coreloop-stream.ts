/**
 * A4: drive one chat turn through the sidecar-hosted Python CoreLoop.
 *
 * This is the default chat path (since 2026-08-26): the sidecar owns
 * think→act→observe, and tool calls come back over the reverse channel
 * (`tool.invoke`, registered in `main.ts`). This module only translates the
 * wire notifications into the existing SSE contract — the renderer, preload
 * and IPC layers are untouched.
 *
 * This is the only chat path (the TS loop was deleted 2026-08-26). If the
 * sidecar failed to start, the router fails loud with a 503 — there is no
 * in-process fallback loop anymore. `STEERABLE_USE_SIDECAR=0` still
 * disables the sidecar process itself (auxiliary LLM calls then fall back
 * to the in-proc providers), but chat requires the sidecar.
 */

import type {
  SidecarChatStreamRequest,
  SidecarChildEvent,
  SidecarRawChunk,
  SidecarSupervisor,
} from '../sidecar/index.js';
import type { LlmMessage } from '../llm/types.js';

export interface CoreLoopToolAction {
  id?: string;
  tool: string;
  arguments: Record<string, unknown>;
  success: boolean;
  result?: string;
  error?: string;
  durationMs?: number;
  /**
   * W4-2: per-exec sandbox marker from `SandboxedToolExecutor`
   * (`data._sandbox` on the tool result). `enforcement: full | partial |
   * none` — rendered on the tool card so the confinement actually applied
   * is user-visible.
   */
  sandbox?: { backend?: string; enforcement: string };
}

export interface CoreLoopTurnOutcome {
  status: string;
  reason?: string;
  /** Sidecar trace id for this turn — the router persists it into
   *  harness_traces so the CoreLoop path leaves the same audit trail as the
   *  TS loop (and dogfood traces can be replayed/diffed). */
  traceId?: string;
  /** W6-9: the run's accumulated billable usage (+ costUsd when priced). */
  usage?: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    cachedPromptTokens?: number;
    costUsd?: number;
  };
}

/**
 * A6 分层披露:本轮技能目录的过滤上下文。sidecar 据此注入 catalog 层
 * (首轮 pre_step 写入系统消息,hook_action 落 trace)并用 `skill` 工具
 * 按需返回技能正文;eager 层正文仍由宿主系统提示词携带。
 */
export interface SkillTurnContext {
  /** 活跃条件串(tool:* / has-tools / plan-mode),与提示词拼装同源。 */
  conditions: string[];
  /**
   * 目录里不出现的技能:模式级排除(plan 模式排掉执行类技能)、智能体关闭
   * 「允许其他技能」时的未勾选项、以及已随系统提示词常驻的勾选项(避免
   * 模型再调 `skill` 工具加载第二遍)。
   */
  exclude: string[];
  /** 智能体的「加载全部技能」:catalog 无视条件全量列出。 */
  ignoreConditions: boolean;
}

export interface StreamCoreLoopTurnOptions {
  supervisor: SidecarSupervisor;
  chatId: string;
  /** W5-2: the chat's active durable record (a branch id after a
   *  regenerate-fork). Undefined = the chatId itself. */
  recordId?: string;
  /**
   * W7-1: continue the durable record's interrupted turn. The sidecar
   * replays the record's projection (dangling tool_calls closed) as the
   * loop seed; `messages` must be empty — the record is authoritative and
   * the host neither re-sends the last user message nor fabricates a
   * synthetic continuation prompt.
   */
  resume?: boolean;
  messages: LlmMessage[];
  tools: Array<{ name: string; description: string; inputSchema: unknown }>;
  provider: string;
  model: string;
  baseUrl?: string;
  apiKey?: string;
  temperature?: number;
  /** W2.8.2: assembled system prompt, sent as a typed fragment param (cap
   * enforced sidecar-side) instead of a leading system message. */
  systemPrompt?: string;
  /** W1.3.2 explicit compat flag overrides, forwarded verbatim; omitted →
   * the sidecar auto-detects from the base-URL host. */
  compat?: SidecarChatStreamRequest['compat'];
  /** Provider-preset choice (auto/off/pinned), forwarded verbatim; omitted →
   * the sidecar auto-matches the llm.presets registry on base-URL+model. */
  presets?: SidecarChatStreamRequest['presets'];
  /** Per-turn reasoning effort from the chat model picker. Forwarded
   * verbatim; the sidecar validates it strict against the model's catalog
   * entry, so an unsupportable level fails the turn at stream start
   * (invalid_params) instead of being silently dropped. */
  reasoningEffort?: string;
  /** Forwarded on every reverse tool.invoke (host enforces plan-mode etc.). */
  toolContext?: Record<string, unknown>;
  /**
   * 4.6a: advertise the sidecar-hosted ask_user tool. Default on for chat
   * turns; background task streams pass `false` — no user is watching the
   * question card, and the task prompt tells the model not to ask.
   */
  askUser?: boolean;
  /**
   * 分层技能披露:skills 根目录 + 本轮过滤上下文。缺省 = 技能机制整体
   * 旁路(显式自定义系统提示词的回合,与旧行为一致)。
   */
  skills?: SkillTurnContext & { roots: string[] };
  /**
   * Wave 2 world-state:慢变宿主上下文(当前时间/时区)作为数据节下发。
   * sidecar 首轮注入 <world-state> 片段,后续轮次与记录里的快照 diff——
   * 未变零 token,变了只追加一个 RFC 7386 小补丁。由 buildWorldState()
   * 构建;缺省 = 不注入(与旧行为一致)。
   */
  worldState?: Record<string, unknown>;
  /**
   * W4-2: per-exec sandbox config forwarded verbatim to the sidecar
   * (`SandboxedToolExecutor`). The router builds it from the chat's project
   * binding (writableRoots = project root) and the provider baseUrl
   * (allowedHosts when network is on).
   */
  execSandbox?: SidecarChatStreamRequest['execSandbox'];
  /**
   * W4-1: approval algebra config forwarded verbatim to the sidecar
   * (`ApprovalExecutor` + `HostApprover`). The router enables host mode;
   * the reverse-channel handler in main.ts drives the Electron UI.
   */
  approval?: SidecarChatStreamRequest['approval'];
  /**
   * P3.1 multi-agent orchestration (六件套), forwarded verbatim to the
   * sidecar. OPT-IN advanced mode: only `enabled: true` exposes
   * agent_spawn/send/wait/close/list/interrupt. Default off since the
   * delegate-on-pool unification — the model's multi-agent surface is
   * `delegate_subagent` (see `subagent`).
   */
  orchestration?: SidecarChatStreamRequest['orchestration'];
  /**
   * delegate-on-pool 委派,原样转发给 sidecar。桌面默认开(`subagent:
   * true`):模型获得单工具 delegate_subagent,子代理作为 AgentPool 池化
   * 子运行执行,生命周期经 `agent.child` 通知(由 `onChildEvent` 上抛)
   * 进编排卡片;concurrent profile 的同轮委派在池预算内并行。
   */
  subagent?: SidecarChatStreamRequest['subagent'];
  /** W6-9: per-turn token budget override. When set, forwarded as
   * `budgetTokens` and the sidecar uses it instead of its rounds-scaled
   * default. */
  budgetTokens?: number;
  signal?: AbortSignal;
  /** Fired once the sidecar streamId is known — needed for steerChat. */
  onStreamId?: (streamId: string) => void;
  /** Assistant text chunk, display-cleaned by the sidecar already. */
  onText: (delta: string) => void;
  /** Thinking-model chain-of-thought delta (sidecar `reasoningDelta`). */
  onReasoning?: (delta: string) => void;
  /** Fired when a tool call starts (running row on the timeline). */
  onToolStart?: (call: { id: string; tool: string; arguments: Record<string, unknown> }) => void;
  /** Fired when a tool finishes on the host (for tool-card rendering). */
  onToolAction?: (action: CoreLoopToolAction) => void;
  /** soft_timeout / budget_exhausted notices from the loop. */
  onNotice?: (kind: string, notice?: { kind: string; [key: string]: unknown }) => void;
  /**
   * Opt-in (`streamRawChunks: true`, default off): every pre-digestion
   * provider chunk the sidecar's `on_stream_chunk` hook observed, before
   * UI-tag stripping produced the display `delta`. For incremental
   * renderers; costs one notification per chunk.
   */
  streamRawChunks?: boolean;
  /** Fired per raw provider chunk when `streamRawChunks` is on. */
  onRawChunk?: (chunk: SidecarRawChunk) => void;
  /** P3.1: child-agent lifecycle events (spawn/complete/fail/interrupt/
   *  resume), demuxed from the sidecar's `agent.child` notifications —
   *  fired by orchestration children and by delegate_subagent delegations. */
  onChildEvent?: (event: SidecarChildEvent) => void;
}

/**
 * Active CoreLoop turns by chatId — lets the main process route a mid-turn
 * steer (user typed while the turn runs) to the right sidecar stream without
 * threading the sidecar streamId up through the router's call chain.
 */
const activeCoreLoopStreams = new Map<string, string>();

// How long to wait for the sidecar's cancelled stream.done (carries the
// traceId) after a user abort before settling with a bare AbortError.
const CANCEL_GRACE_MS = 10_000;

/** Sidecar streamId of the chat's running CoreLoop turn, if any. */
export function getActiveCoreLoopStreamId(chatId: string): string | undefined {
  return activeCoreLoopStreams.get(chatId);
}

const WEEKDAYS_ZH = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as const;

/**
 * W6-7b world-state 扩面的输入。除 time 外各节都可缺省——缺省的节不出现在
 * 快照里,与"只喂了 time 一节"的旧行为一致。各节都是小型对比数据(不是
 * 正文),sidecar 逐节 merge-patch diff:没变零 token,变了只补一个小补丁。
 */
export interface WorldStateInput {
  /** 当前时间(可注入以便测试);缺省 = 现在。 */
  now?: Date;
  /** 协作模式(agent/plan)。模型据此明确当前模式,而不只靠系统提示词。 */
  mode?: 'agent' | 'plan';
  /**
   * 权限姿态:approval 模式 + 执行沙箱。让模型事先知道哪些目录可写、网络
   * 是否开、工具调用是否要过审批,避免去尝试必然被拦的操作。
   */
  permissions?: {
    approval: 'host' | 'off';
    sandbox: { enabled: boolean; writableRoots: string[]; network: boolean };
  };
  /**
   * 技能姿态(紧凑):本轮活跃条件 + 模式级排除清单。只放过滤上下文,不放
   * 技能正文——正文由分层披露(catalog/eager)机制携带,这里给模型一个
   * "当前技能过滤面"的可 diff 数据视图。
   */
  skills?: { conditions: string[]; exclude: string[] };
  /**
   * 场景包附加节（1.2 pack 回合钩子）：原样并入输出。包用包前缀键
   * （如 `<pack>_workspace`）避免与 shell 节碰撞。
   */
  extra?: Record<string, unknown>;
}

/**
 * 构建本轮的 world-state 节(Wave 2 起;W6-7b 从只有 time 扩到多节)。
 *
 * time 节分钟精度是刻意的:模型不需要秒,而快照在同一分钟内稳定,sidecar
 * 的 merge-patch diff 对连续轮次就是零 token 空转;跨分钟只追加一个几
 * token 的尾部补丁,系统提示词前缀保持字节稳定(cache 净收益)。其余各节
 * (mode/permissions/skills)同理——慢变,变了才补一个小补丁。
 */
export function buildWorldState(input: WorldStateInput = {}): Record<string, unknown> {
  const now = input.now ?? new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  const local =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const state: Record<string, unknown> = {
    time: {
      local,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      weekday: WEEKDAYS_ZH[now.getDay()],
    },
  };
  if (input.mode) {
    state.mode = { name: input.mode };
  }
  if (input.permissions) {
    state.permissions = {
      approval: input.permissions.approval,
      sandbox: {
        enabled: input.permissions.sandbox.enabled,
        writableRoots: input.permissions.sandbox.writableRoots,
        network: input.permissions.sandbox.network,
      },
    };
  }
  if (input.skills) {
    state.skills = {
      conditions: input.skills.conditions,
      exclude: input.skills.exclude,
    };
  }
  if (input.extra) {
    Object.assign(state, input.extra);
  }
  return state;
}

export async function streamCoreLoopTurn(
  options: StreamCoreLoopTurnOptions,
): Promise<CoreLoopTurnOutcome> {
  const { supervisor, signal } = options;

  if (signal?.aborted) {
    throw new DOMException('The operation was aborted.', 'AbortError');
  }

  const request: SidecarChatStreamRequest = {
    provider: options.provider,
    model: options.model,
    baseUrl: options.baseUrl,
    apiKey: options.apiKey,
    temperature: options.temperature,
    systemPrompt: options.systemPrompt,
    compat: options.compat,
    presets: options.presets,
    ...(options.reasoningEffort ? { reasoningEffort: options.reasoningEffort } : {}),
    chatId: options.chatId,
    recordId: options.recordId,
    resume: options.resume,
    useCoreLoop: true,
    // Stream every CoreLoop round's display text so the UI can interleave
    // reasoning, tool cards, and replies in call order. Retry drafts still
    // live in the durable record/trace; the renderer places each round's
    // narration next to the tools that followed it.
    contentMode: 'all',
    toolsViaHost: true,
    streamRawChunks: options.streamRawChunks,
    // W8: advertise the sidecar-hosted ask_user tool; the desktop answers
    // `ask_user.request` reverse calls with the renderer question card
    // (AskUserModalHost). Under toolsViaHost the sidecar intercepts
    // ask_user locally, so it never reaches the host's tool.invoke.
    // 4.6a: background task streams opt out (options.askUser === false).
    askUser: options.askUser ?? true,
    toolContext: options.toolContext,
    // Parity with the TS loop: the four anti-hallucination guards (data-need
    // routing, deferred/claimed retry, grounding judge, narration round) run
    // as CoreLoop hooks on the sidecar side.
    antiHallucination: true,
    skills: options.skills,
    worldState: options.worldState,
    execSandbox: options.execSandbox,
    approval: options.approval,
    orchestration: options.orchestration,
    subagent: options.subagent,
    budgetTokens: options.budgetTokens,
    messages: options.messages.map((m) => {
      // W6-3: a message carrying images is sent as structured `parts` (the
      // sidecar treats `parts` as authoritative, `content` as its text
      // projection). Text-only messages keep the legacy `content` shorthand.
      const base = {
        role: m.role,
        content: m.content,
        name: m.name,
        toolCallId: m.toolCallId,
      };
      if (m.images && m.images.length > 0) {
        return {
          ...base,
          parts: [
            { type: 'text' as const, text: m.content },
            ...m.images.map((img) => ({
              type: 'image' as const,
              data: img.data,
              mediaType: img.mediaType,
            })),
          ],
        };
      }
      return base;
    }),
    tools: options.tools.map((t) => ({
      type: 'function',
      function: {
        name: t.name,
        description: t.description,
        parameters: t.inputSchema ?? { type: 'object', properties: {} },
      },
    })),
  };

  // Track in-flight tool calls so results can be paired with their arguments
  // (the start notification carries them, the result notification does not).
  const pendingCalls = new Map<string, { name: string; arguments: Record<string, unknown> }>();

  return await new Promise<CoreLoopTurnOutcome>((resolve, reject) => {
    let streamIdRef: string | null = null;
    let settled = false;
    let cancelling = false;
    let abortGraceTimer: ReturnType<typeof setTimeout> | null = null;

    const onAbort = () => {
      if (settled || cancelling) return;
      cancelling = true;
      if (streamIdRef) {
        void supervisor.cancelChat(streamIdRef);
      }
      // Don't reject yet: the sidecar answers the cancel with a stream.done
      // carrying status 'cancelled' AND the traceId — resolving on it lets
      // the router persist the partial trace (cancelled turns are dogfood
      // signal too). Only if the done never arrives do we settle bare.
      abortGraceTimer = setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(new DOMException('The operation was aborted.', 'AbortError'));
      }, CANCEL_GRACE_MS);
    };
    signal?.addEventListener('abort', onAbort);
    const cleanup = () => {
      signal?.removeEventListener('abort', onAbort);
      if (abortGraceTimer) clearTimeout(abortGraceTimer);
      activeCoreLoopStreams.delete(options.chatId);
    };

    void supervisor
      .streamChat(request, {
        onChunk: (chunk) => {
          // The user cancelled — swallow any in-flight deltas while we wait
          // for the sidecar's cancelled done.
          if (cancelling) return;
          if (chunk.delta) {
            options.onText(chunk.delta);
          }
          if (chunk.reasoningDelta) {
            options.onReasoning?.(chunk.reasoningDelta);
          }
          if (chunk.toolCall) {
            pendingCalls.set(chunk.toolCall.id, {
              name: chunk.toolCall.name,
              arguments: chunk.toolCall.arguments ?? {},
            });
            options.onToolStart?.({
              id: chunk.toolCall.id,
              tool: chunk.toolCall.name,
              arguments: chunk.toolCall.arguments ?? {},
            });
          }
          if (chunk.toolResult) {
            const started = pendingCalls.get(chunk.toolResult.id);
            pendingCalls.delete(chunk.toolResult.id);
            options.onToolAction?.({
              id: chunk.toolResult.id,
              tool: chunk.toolResult.name,
              arguments: started?.arguments ?? {},
              success: chunk.toolResult.success,
              result: chunk.toolResult.resultPreview,
              error: chunk.toolResult.error,
              durationMs: chunk.toolResult.durationMs,
              sandbox: chunk.toolResult.sandbox,
            });
          }
          if (chunk.notice) {
            options.onNotice?.(chunk.notice.kind, chunk.notice);
          }
          if (chunk.rawChunk) {
            options.onRawChunk?.(chunk.rawChunk);
          }
        },
        onDone: (done) => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve({
            status:
              done.status ?? (done.cancelled ? 'cancelled' : done.ok ? 'completed' : 'failed'),
            reason: done.reason ?? (done.cancelled ? 'aborted_by_user' : undefined),
            traceId: done.traceId,
            usage: done.usage,
          });
        },
        onChildEvent: (event) => {
          if (settled || cancelling) return;
          options.onChildEvent?.(event);
        },
        onError: (err) => {
          if (settled) return;
          settled = true;
          cleanup();
          // Carry the sidecar trace id on the rejection so the router can
          // persist the partial trace — failed turns are the ones dogfooding
          // most needs to inspect.
          const failure = new Error(`coreloop stream failed: ${err.kind}: ${err.message}`);
          if (err.traceId) (failure as Error & { traceId?: string }).traceId = err.traceId;
          reject(failure);
        },
      })
      .then((streamId) => {
        streamIdRef = streamId;
        activeCoreLoopStreams.set(options.chatId, streamId);
        options.onStreamId?.(streamId);
        if (signal?.aborted && !settled) onAbort();
      })
      .catch((err) => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(err);
      });
  });
}
