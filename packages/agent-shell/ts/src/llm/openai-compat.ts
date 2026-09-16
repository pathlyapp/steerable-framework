import { randomUUID } from 'crypto';
import type { LlmGenerateRequest, LlmGenerateResult, LlmMessage, LlmProvider, LlmToolSchema } from './types.js';
import {
  isForcedToolChoiceRejected,
  rememberForcedToolChoiceRejected,
  resolveOpenAiToolChoice,
} from './tool-choice.js';

export {
  canonicalizeModelId,
  isForcedToolChoiceRejected,
  modelLikelyRejectsForcedToolChoice,
  rejectsForcedToolChoice,
  rememberForcedToolChoiceRejected,
  resetForcedToolChoiceCompatForTests,
  resolveOpenAiToolChoice,
} from './tool-choice.js';

interface OpenAICompatProviderOptions {
  baseUrl: string;
  apiKey?: string;
  model: string;
  temperature?: number;
}

interface OpenAiChatCompletionResponse {
  usage?: {
    prompt_tokens?: number;
    completion_tokens?: number;
    total_tokens?: number;
  };
  choices?: Array<{
    message?: {
      content?: string | null;
      // DeepSeek thinking 模式特有；其它 provider 没这个字段。
      reasoning_content?: string | null;
      tool_calls?: Array<{
        id?: string;
        function?: {
          name?: string;
          arguments?: string;
        };
      }>;
    };
  }>;
}

export class OpenAICompatProvider implements LlmProvider {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly model: string;
  private readonly temperature?: number;

  constructor(options: OpenAICompatProviderOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '');
    this.apiKey = options.apiKey;
    this.model = options.model;
    this.temperature = options.temperature;
  }

  async listModels(): Promise<string[]> {
    try {
      const headers = new Headers();
      if (this.apiKey) {
        headers.set('Authorization', `Bearer ${this.apiKey}`);
      }
      const res = await fetch(`${this.baseUrl}/v1/models`, { headers });
      if (!res.ok) {
        return [this.model];
      }
      const data = (await res.json()) as { data?: Array<{ id?: string }> };
      const models = (data.data || []).map(item => item.id).filter((item): item is string => Boolean(item));
      return models.length ? models : [this.model];
    } catch {
      return [this.model];
    }
  }

  async generate(request: LlmGenerateRequest): Promise<LlmGenerateResult> {
    const headers = new Headers({
      'Content-Type': 'application/json',
    });
    if (this.apiKey) {
      headers.set('Authorization', `Bearer ${this.apiKey}`);
    }

    const payload: Record<string, unknown> = {
      model: request.model || this.model,
      messages: request.messages.map(message => this.mapMessage(message)),
      temperature: request.temperature ?? this.temperature ?? 0.3,
      stream: false,
    };

    const tools = this.mapTools(request.tools || []);
    if (tools.length) {
      payload.tools = tools;
      // harness 路由判定"需要本轮新数据"时传 'required'。thinking/reasoner
      // 模型（以及上次 400 过的未知模型）会在 resolveOpenAiToolChoice 里降成 auto。
      payload.tool_choice = resolveOpenAiToolChoice(
        String(payload.model),
        request.toolChoice,
      );
    }

    const endpoint = `${this.baseUrl}/v1/chat/completions`;
    const maxAttempts = payload.tool_choice === 'required' ? 2 : 1;
    let lastStatus = 0;
    let lastErrorText = '';

    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      let res: Response;
      try {
        res = await fetch(endpoint, {
          method: 'POST',
          headers,
          body: JSON.stringify(payload),
          signal: request.signal,
        });
      } catch (err) {
        if (request.signal?.aborted) {
          throw err instanceof Error ? err : new Error(String(err));
        }
        const cause = err instanceof Error ? err.message : String(err);
        throw new Error(
          `无法连接 OpenAI 兼容网关 (${this.baseUrl})，请检查 baseUrl / 网络。原始错误: ${cause}`
        );
      }

      if (res.ok) {
        const data = (await res.json()) as OpenAiChatCompletionResponse;
        return this.mapResult(data);
      }

      lastErrorText = await res.text();
      lastStatus = res.status;
      const canRetry =
        attempt === 0 &&
        payload.tool_choice === 'required' &&
        isForcedToolChoiceRejected(res.status, lastErrorText);
      if (!canRetry) break;

      rememberForcedToolChoiceRejected(String(payload.model));
      payload.tool_choice = 'auto';
      console.info('[openai-compat] tool_choice=required rejected; retrying with auto', {
        model: payload.model,
        status: res.status,
      });
    }

    const ctx = `model=${payload.model} baseUrl=${this.baseUrl}`;
    if (lastStatus === 401 || lastStatus === 403) {
      throw new Error(`OpenAI 兼容网关鉴权失败 (${lastStatus}) [${ctx}]，请检查 API Key。`);
    }
    throw new Error(`OpenAI 兼容请求失败: ${lastStatus} [${ctx}] ${lastErrorText}`);
  }

  private mapResult(data: OpenAiChatCompletionResponse): LlmGenerateResult {
    const message = data.choices?.[0]?.message;
    const content = message?.content || '';
    const reasoningContent = message?.reasoning_content || undefined;
    const toolCalls = (message?.tool_calls || []).map(item => {
      const fnArgs = item.function?.arguments || '{}';
      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(fnArgs);
      } catch {
        parsed = {};
      }
      return {
        id: item.id || randomUUID(),
        name: item.function?.name || 'unknown_tool',
        arguments: parsed,
      };
    });

    return {
      content,
      toolCalls,
      reasoningContent,
      tokensUsed: {
        prompt: Number(data.usage?.prompt_tokens || 0),
        completion: Number(data.usage?.completion_tokens || 0),
        total: Number(data.usage?.total_tokens || 0),
      },
      raw: data,
    };
  }

  private mapTools(tools: LlmToolSchema[]): Array<Record<string, unknown>> {
    return tools.map(tool => ({
      type: 'function',
      function: {
        name: tool.name,
        description: tool.description,
        parameters: tool.inputSchema || {
          type: 'object',
          properties: {},
          additionalProperties: true,
        },
      },
    }));
  }

  private mapMessage(message: LlmMessage): Record<string, unknown> {
    if (message.role === 'tool') {
      return {
        role: 'tool',
        content: message.content,
        tool_call_id: message.toolCallId || message.name || 'tool',
      };
    }
    // assistant 消息：如果本轮触发了工具调用，必须把 tool_calls 一起发出去，
    // 否则下一轮 messages 里的 `role: 'tool'` 会被 OpenAI 严格协议判为"孤儿"
    // 报 400 (Messages with role 'tool' must be a response to a preceding
    // message with 'tool_calls')。content 在这种情况下允许为空字符串。
    //
    // thinking 模式还要求 reasoning_content 必须原样回传——见
    // LlmMessage.reasoningContent。非 thinking 模型该字段为 undefined，跳过
    // 不影响兼容。
    if (message.role === 'assistant') {
      const out: Record<string, unknown> = {
        role: 'assistant',
        content: message.content || '',
      };
      if (message.toolCalls && message.toolCalls.length > 0) {
        out.tool_calls = message.toolCalls.map((call) => ({
          id: call.id,
          type: 'function',
          function: {
            name: call.name,
            arguments: JSON.stringify(call.arguments ?? {}),
          },
        }));
      }
      if (message.reasoningContent) {
        out.reasoning_content = message.reasoningContent;
      }
      return out;
    }
    // W6-3: user messages carrying images use the OpenAI vision content-array
    // form (`image_url` with a data URL). Text-only messages keep the plain
    // string shorthand so existing wire bytes are unchanged.
    if (message.role === 'user' && message.images && message.images.length > 0) {
      return {
        role: 'user',
        content: [
          { type: 'text', text: message.content },
          ...message.images.map((img) => ({
            type: 'image_url',
            image_url: { url: `data:${img.mediaType};base64,${img.data}` },
          })),
        ],
      };
    }
    return {
      role: message.role,
      content: message.content,
    };
  }
}
