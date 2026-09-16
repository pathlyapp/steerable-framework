export type LlmRole = 'system' | 'user' | 'assistant' | 'tool';

/**
 * W6-3: an image attached to a message, carried as base64 bytes. Produced
 * host-side (see `image-attachment.ts`, which enforces byte/dimension caps
 * and downscales) and serialized per-provider — OpenAI-compat as an
 * `image_url` data-URL content part, the sidecar CoreLoop as a wire
 * `ContentPart` (`{type:'image', data, mediaType}`).
 */
export interface LlmImage {
  /** base64-encoded image bytes (no `data:` prefix). */
  data: string;
  /** MIME type, e.g. `image/png` / `image/jpeg`. */
  mediaType: string;
}

export interface LlmMessage {
  role: LlmRole;
  content: string;
  /**
   * W6-3: images attached to this message (typically the current user
   * turn). Kept out of `content` so the text projection stays clean for
   * persistence/compaction; providers that support vision serialize these
   * alongside the text.
   */
  images?: LlmImage[];
  /** 仅 `role: 'tool'` 用，指向被回应的 assistant tool_call 的 id。 */
  toolCallId?: string;
  /** 仅 `role: 'tool'` 用，工具名（Ollama 兼容字段，OpenAI 严格协议忽略）。 */
  name?: string;
  /**
   * 仅 `role: 'assistant'` 用：本轮 LLM 触发的工具调用。
   *
   * 为什么必须显式带：OpenAI 严格协议要求 `role: 'tool'` 消息**必须**紧跟在
   * 一个带 `tool_calls` 字段的 assistant 消息之后；否则报
   *   `Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`
   * （400）。早期实现只在 messages 里 push 一条 `{role:'assistant', content:''}`
   * 占位 + 一条 `{role:'tool', ...}`，丢了 tool_calls 字段——Ollama 容忍这种
   * 不规范序列，DeepSeek/OpenAI 严格校验直接拒。
   */
  toolCalls?: LlmToolCall[];
  /**
   * 仅 `role: 'assistant'` 用：DeepSeek thinking 模式 (deepseek-reasoner /
   * deepseek-v4-*) 在 response.message 里会带一个 `reasoning_content` 字段
   * 表示"思考链"。下一轮 API 调用时必须把它**原样塞回去**，否则报
   *   "The `reasoning_content` in the thinking mode must be passed back to the API."
   * （400）。非 thinking 模型（deepseek-chat / 通用 OpenAI 模型）此字段为空，
   * provider 会跳过不发，不影响兼容。
   */
  reasoningContent?: string;
}

export interface LlmToolSchema {
  name: string;
  description: string;
  inputSchema?: Record<string, unknown>;
}

export interface LlmToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface LlmGenerateRequest {
  messages: LlmMessage[];
  model: string;
  temperature?: number;
  tools?: LlmToolSchema[];
  /**
   * 工具选择约束（OpenAI 兼容 API 的 tool_choice）：
   *   - 'auto'（默认）：模型自行决定是否调用工具；
   *   - 'required'：本轮**必须**至少发起一个工具调用——用于 harness 路由判定
   *     "该问题需要本轮新数据"后，从机制上保证模型先调工具再作答，
   *     让"零工具编造数据"在生成阶段就不可能发生。
   *   - 'none'：禁止调用工具。
   * 仅 OpenAI 兼容 provider 支持；Ollama / sidecar 目前忽略此字段。
   *
   * thinking / reasoner 模型普遍不接受 `required`。OpenAICompatProvider 会
   * 按能力启发式或 400 降级把它改成 `auto`，tools 仍照常下发。
   */
  toolChoice?: 'auto' | 'none' | 'required';
  /**
   * Cancellation signal. The router checks `signal.aborted` *between* turns
   * and tool calls, but without threading it into the actual HTTP request,
   * cancelling mid-generation didn't stop the in-flight LLM call — it just
   * kept streaming tokens (and burning provider quota) until the response
   * finished on its own, only then noticing the abort before the *next*
   * turn. Providers should pass this straight into `fetch()`'s `signal`
   * option (or the sidecar-RPC equivalent) so aborting actually tears down
   * the underlying request.
   */
  signal?: AbortSignal;
}

export interface LlmGenerateResult {
  content: string;
  toolCalls: LlmToolCall[];
  /**
   * DeepSeek thinking 模式响应里的 `reasoning_content`（思考链文本）。
   * 非 thinking 模型 / 非 DeepSeek 服务返回 undefined。需要在下一轮请求时
   * 原样回传，见 LlmMessage.reasoningContent。
   */
  reasoningContent?: string;
  tokensUsed?: {
    prompt: number;
    completion: number;
    total: number;
  };
  raw?: unknown;
}

export interface LlmStreamCallbacks {
  /** 每收到一个新的文本片段时回调，可能多次。tool-call 阶段不回调。 */
  onToken?: (token: string) => void;
}

export interface LlmGenerateStreamRequest extends LlmGenerateRequest {
  callbacks?: LlmStreamCallbacks;
}

export interface LlmProvider {
  listModels(): Promise<string[]>;
  generate(request: LlmGenerateRequest): Promise<LlmGenerateResult>;
  /**
   * 真正的流式生成。Provider 内部建议走原生 SSE / NDJSON，
   * 在每个增量到达时触发 `callbacks.onToken`，最后再返回聚合结果。
   * 默认实现可以转调 generate（无流式）。
   */
  generateStream?(request: LlmGenerateStreamRequest): Promise<LlmGenerateResult>;
}
