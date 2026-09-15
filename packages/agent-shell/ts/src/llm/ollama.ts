import { randomUUID } from 'crypto';
import type {
  LlmGenerateRequest,
  LlmGenerateResult,
  LlmGenerateStreamRequest,
  LlmProvider,
} from './types.js';

interface OllamaProviderOptions {
  baseUrl?: string;
  model: string;
  temperature?: number;
}

interface OllamaTagResponse {
  models?: Array<{ name?: string }>;
}

interface OllamaChatResponse {
  prompt_eval_count?: number;
  eval_count?: number;
  done_reason?: string;
  message?: {
    content?: string;
    tool_calls?: Array<{
      function?: {
        name?: string;
        arguments?: Record<string, unknown> | string;
      };
    }>;
  };
}

export class OllamaProvider implements LlmProvider {
  private readonly baseUrl: string;
  private readonly model: string;
  private readonly temperature?: number;

  constructor(options: OllamaProviderOptions) {
    this.baseUrl = (options.baseUrl || 'http://127.0.0.1:11434').replace(/\/$/, '');
    this.model = options.model;
    this.temperature = options.temperature;
  }

  async listModels(): Promise<string[]> {
    try {
      const res = await fetch(`${this.baseUrl}/api/tags`);
      if (!res.ok) return [this.model];
      const data = (await res.json()) as OllamaTagResponse;
      const models = (data.models || []).map(item => item.name).filter((item): item is string => Boolean(item));
      return models.length ? models : [this.model];
    } catch {
      return [this.model];
    }
  }

  async generate(request: LlmGenerateRequest): Promise<LlmGenerateResult> {
    const payload = this.buildPayload(request, false);

    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}/api/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
        signal: request.signal,
      });
    } catch (err) {
      if (request.signal?.aborted) {
        throw err instanceof Error ? err : new Error(String(err));
      }
      const cause = err instanceof Error ? err.message : String(err);
      throw new Error(
        `无法连接本地 Ollama (${this.baseUrl})。请确认已运行 \`ollama serve\` 并执行过 \`ollama pull ${this.model}\`。原始错误: ${cause}`
      );
    }

    if (!res.ok) {
      const text = await res.text();
      if (res.status === 404 && /model.*not found/i.test(text)) {
        throw new Error(
          `Ollama 模型 ${this.model} 未下载，请执行 \`ollama pull ${this.model}\` 后重试。`
        );
      }
      throw new Error(`Ollama 请求失败: ${res.status} ${text}`);
    }

    const data = (await res.json()) as OllamaChatResponse;
    return this.parseFinalMessage(data.message, data.prompt_eval_count, data.eval_count);
  }

  /**
   * NDJSON 流式生成。Ollama 会按 chunk 推 `{message:{content:"x"}, done:false}`，
   * 最后一行 `{done:true, message:{tool_calls?:[]}, ...}`。
   * 我们只在收到文本片段时回调 onToken；tool_calls 通常在 done 帧里完整给出。
   */
  async generateStream(request: LlmGenerateStreamRequest): Promise<LlmGenerateResult> {
    const payload = this.buildPayload(request, true);

    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: request.signal,
      });
    } catch (err) {
      if (request.signal?.aborted) {
        throw err instanceof Error ? err : new Error(String(err));
      }
      const cause = err instanceof Error ? err.message : String(err);
      throw new Error(
        `无法连接本地 Ollama (${this.baseUrl})。请确认已运行 \`ollama serve\` 并执行过 \`ollama pull ${this.model}\`。原始错误: ${cause}`
      );
    }

    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '');
      if (res.status === 404 && /model.*not found/i.test(text)) {
        throw new Error(
          `Ollama 模型 ${this.model} 未下载，请执行 \`ollama pull ${this.model}\` 后重试。`
        );
      }
      throw new Error(`Ollama 请求失败: ${res.status} ${text}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let aggregatedContent = '';
    let finalMessage: OllamaChatResponse['message'] | undefined;
    // tool_calls 在 stream 模式下可能在任意一帧到达；逐帧累积，
    // 不能只看 done 帧（done 帧的 message 经常只有 content:'' 没有 tool_calls）。
    type OllamaToolCall = NonNullable<NonNullable<OllamaChatResponse['message']>['tool_calls']>[number];
    const aggregatedToolCalls: OllamaToolCall[] = [];
    let frameCount = 0;
    let lastFrameSample: unknown = undefined;
    let promptEvalCount = 0;
    let evalCount = 0;

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nlIndex: number;
        while ((nlIndex = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, nlIndex).trim();
          buffer = buffer.slice(nlIndex + 1);
          if (!line) continue;
          let frame: {
            message?: OllamaChatResponse['message'];
            done?: boolean;
            done_reason?: string;
            prompt_eval_count?: number;
            eval_count?: number;
          };
          try {
            frame = JSON.parse(line);
          } catch {
            continue;
          }
          frameCount++;
          const piece = frame.message?.content || '';
          if (piece) {
            aggregatedContent += piece;
            request.callbacks?.onToken?.(piece);
          }
          const calls = frame.message?.tool_calls;
          if (calls && calls.length) {
            aggregatedToolCalls.push(...calls);
          }
          if (typeof frame.prompt_eval_count === 'number') {
            promptEvalCount = frame.prompt_eval_count;
          }
          if (typeof frame.eval_count === 'number') {
            evalCount = frame.eval_count;
          }
          if (frame.done) {
            finalMessage = frame.message;
            lastFrameSample = frame;
          }
        }
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        // best-effort
      }
    }

    const mergedToolCalls = aggregatedToolCalls.length
      ? aggregatedToolCalls
      : finalMessage?.tool_calls;
    console.info('[ollama stream] done', {
      model: this.model,
      frames: frameCount,
      contentBytes: aggregatedContent.length,
      toolCallCount: mergedToolCalls?.length || 0,
      doneReason: (lastFrameSample as { done_reason?: string } | undefined)?.done_reason,
    });
    if (!aggregatedContent && !(mergedToolCalls && mergedToolCalls.length)) {
      // 把最后一帧打出来便于排查
      console.warn('[ollama stream] empty response, lastFrame=', JSON.stringify(lastFrameSample));
    }
    return this.parseFinalMessage(
      {
        content: aggregatedContent || finalMessage?.content || '',
        tool_calls: mergedToolCalls,
      },
      promptEvalCount,
      evalCount
    );
  }

  private buildPayload(request: LlmGenerateRequest, stream: boolean): Record<string, unknown> {
    const payload: Record<string, unknown> = {
      model: request.model || this.model,
      messages: request.messages.map(message => {
        // Ollama 容忍 assistant 消息没 tool_calls，但为了和 OpenAI 严格协议
        // 兼容（同一份 LlmMessage[] 喂给两种 provider 都该工作）这里也带上。
        // Ollama 0.4+ 期望 arguments 是对象，OpenAI 期望 JSON string；故此处
        // 直接传对象不要 stringify。
        if (message.role === 'assistant' && message.toolCalls && message.toolCalls.length > 0) {
          return {
            role: 'assistant',
            content: message.content || '',
            tool_calls: message.toolCalls.map((call) => ({
              function: {
                name: call.name,
                arguments: call.arguments ?? {},
              },
            })),
          };
        }
        return {
          role: message.role === 'tool' ? 'tool' : message.role,
          content: message.content,
          ...(message.role === 'tool' && message.name ? { name: message.name } : {}),
        };
      }),
      stream,
      options: {
        temperature: request.temperature ?? this.temperature ?? 0.3,
      },
    };
    if ((request.tools || []).length > 0) {
      payload.tools = (request.tools || []).map(tool => ({
        type: 'function',
        function: {
          name: tool.name,
          description: tool.description,
          parameters: tool.inputSchema || {
            type: 'object',
            properties: {},
          },
        },
      }));
    }
    return payload;
  }

  private parseFinalMessage(
    message: OllamaChatResponse['message'] | undefined,
    promptEvalCount = 0,
    evalCount = 0
  ): LlmGenerateResult {
    const content = message?.content || '';
    const toolCalls = (message?.tool_calls || []).map(item => {
      const args = item.function?.arguments;
      let parsed: Record<string, unknown> = {};
      if (typeof args === 'string') {
        try {
          parsed = JSON.parse(args);
        } catch {
          parsed = {};
        }
      } else if (args && typeof args === 'object') {
        parsed = args;
      }
      return {
        id: randomUUID(),
        name: item.function?.name || 'unknown_tool',
        arguments: parsed,
      };
    });
    return {
      content,
      toolCalls,
      tokensUsed: {
        prompt: promptEvalCount,
        completion: evalCount,
        total: promptEvalCount + evalCount,
      },
      raw: message,
    };
  }
}
