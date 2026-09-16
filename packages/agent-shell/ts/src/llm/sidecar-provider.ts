/**
 * `LlmProvider` implementation that delegates to the steerable-sidecar's
 * `agent.chat.stream` JSON-RPC method.
 *
 * Activation:
 *   Default-on once the sidecar is supervised (see `llm/index.ts`);
 *   `STEERABLE_USE_SIDECAR=0` opts back out to in-process providers.
 *
 * The sidecar must already be supervised by the main process (see
 * `src/sidecar/supervisor.ts`); this provider just routes calls through the
 * supplied supervisor handle and aggregates streamed chunks back into the
 * existing `LlmGenerateResult` shape so the rest of the local backend (router,
 * harness, transcript) keeps working unchanged.
 *
 * NOTE: this is the first half of P5-agent-refactor. Tools are still dispatched
 * by the local TS `ToolRouter`; tool-call delegation to the sidecar's
 * `tool.invoke` method is tracked under p5-agent-refactor-phase2.
 */

import type { SidecarSupervisor, SidecarStreamChunk } from '../sidecar/index.js';
import type {
  LlmGenerateRequest,
  LlmGenerateResult,
  LlmGenerateStreamRequest,
  LlmProvider,
  LlmToolCall,
} from './types.js';

export interface SidecarProviderOptions {
  supervisor: SidecarSupervisor;
  /** Wire-protocol provider name expected by the sidecar (openai_compat | anthropic | ollama). */
  provider: string;
  model: string;
  baseUrl?: string;
  apiKey?: string;
  temperature?: number;
}

export class SidecarLlmProvider implements LlmProvider {
  constructor(private readonly options: SidecarProviderOptions) {}

  async listModels(): Promise<string[]> {
    // The sidecar serves the gateway's live catalog (`models.list`); the
    // configured model stays as fallback for the offline state so callers
    // of this plain-id interface never see an empty list.
    try {
      const catalog = await this.options.supervisor.listModels({
        baseUrl: this.options.baseUrl,
        apiKey: this.options.apiKey,
        provider: this.options.provider,
      });
      if (catalog.models.length > 0) {
        return catalog.models.map((entry) => entry.id);
      }
    } catch {
      // transport/timeout — fall through to the configured model
    }
    return [this.options.model];
  }

  async generate(request: LlmGenerateRequest): Promise<LlmGenerateResult> {
    return this._stream(request);
  }

  async generateStream(request: LlmGenerateStreamRequest): Promise<LlmGenerateResult> {
    return this._stream(request);
  }

  private async _stream(request: LlmGenerateStreamRequest | LlmGenerateRequest): Promise<LlmGenerateResult> {
    const callbacks = (request as LlmGenerateStreamRequest).callbacks;
    const signal = request.signal;
    let assembled = '';
    const toolCalls: LlmToolCall[] = [];
    let tokensUsed: LlmGenerateResult['tokensUsed'];

    if (signal?.aborted) {
      throw new DOMException('The operation was aborted.', 'AbortError');
    }

    return await new Promise<LlmGenerateResult>((resolve, reject) => {
      let streamIdRef: string | null = null;
      let settled = false;

      // `streamChat` only starts rejecting `stream.chunk`/`stream.done` events
      // once it has a `streamId`; if the caller aborts before that resolves
      // there's nothing to cancel yet, so we forward the abort as soon as it's
      // available instead of silently letting the sidecar keep generating.
      const onAbort = () => {
        if (settled) return;
        settled = true;
        if (streamIdRef) {
          void this.options.supervisor.cancelChat(streamIdRef);
        }
        reject(new DOMException('The operation was aborted.', 'AbortError'));
      };
      signal?.addEventListener('abort', onAbort);
      const cleanup = () => signal?.removeEventListener('abort', onAbort);

      void this.options.supervisor
        .streamChat(
          {
            provider: this.options.provider,
            model: request.model || this.options.model,
            baseUrl: this.options.baseUrl,
            apiKey: this.options.apiKey,
            temperature: request.temperature ?? this.options.temperature,
            messages: request.messages.map((m) => ({
              role: m.role,
              content: m.content,
              name: m.name,
              toolCallId: m.toolCallId,
              // 工具调用与推理必须随 assistant 消息回传：缺 toolCalls 时
              // 下一轮 role:'tool' 被判孤儿 400；thinking 模型（DeepSeek）
              // 缺 reasoningContent 同样 400。in-process openai-compat.ts
              // 的 mapMessage 是同一约束的参照实现。
              toolCalls: m.toolCalls?.map((c) => ({
                id: c.id,
                name: c.name,
                arguments: c.arguments,
              })),
              reasoningContent: m.reasoningContent,
            })),
            tools: request.tools?.map((t) => ({
              type: 'function',
              function: {
                name: t.name,
                description: t.description,
                parameters: t.inputSchema ?? { type: 'object', properties: {} },
              },
            })),
          },
          {
            onChunk: (chunk: SidecarStreamChunk) => {
              if (chunk.delta) {
                assembled += chunk.delta;
                callbacks?.onToken?.(chunk.delta);
              }
              if (chunk.toolCall) {
                toolCalls.push({
                  id: chunk.toolCall.id,
                  name: chunk.toolCall.name,
                  arguments: chunk.toolCall.arguments,
                });
              }
              if (chunk.usage) {
                tokensUsed = {
                  prompt: chunk.usage.promptTokens,
                  completion: chunk.usage.completionTokens,
                  total: chunk.usage.totalTokens,
                };
              }
            },
            onDone: () => {
              if (settled) return;
              settled = true;
              cleanup();
              resolve({
                content: assembled,
                toolCalls,
                tokensUsed,
              });
            },
            onError: (err) => {
              if (settled) return;
              settled = true;
              cleanup();
              reject(new Error(`sidecar chat stream failed: ${err.kind}: ${err.message}`));
            },
          },
        )
        .then((streamId) => {
          streamIdRef = streamId;
          // Abort arrived while `streamChat`'s initial RPC round-trip was
          // still in flight — cancel it now that we finally have an id.
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
}
