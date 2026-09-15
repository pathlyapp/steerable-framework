import { describe, expect, it, vi } from 'vitest';
import { SidecarLlmProvider } from '../../src/llm/sidecar-provider';
import type {
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
} from '../../src/sidecar';

interface FakeSupervisor {
  streamChat: ReturnType<typeof vi.fn>;
  cancelChat: ReturnType<typeof vi.fn>;
}

function createFakeSupervisor(
  scenario: 'happy' | 'tool-call' | 'error' | 'cancel' | 'never-settles',
): FakeSupervisor {
  const supervisor: FakeSupervisor = {
    streamChat: vi.fn(),
    cancelChat: vi.fn(async () => undefined),
  };

  supervisor.streamChat.mockImplementation(
    async (req: SidecarChatStreamRequest, handlers: SidecarChatStreamHandlers) => {
      const streamId = req.streamId ?? 'str_test';
      // Run handlers asynchronously so the provider can subscribe first.
      queueMicrotask(() => {
        if (scenario === 'happy') {
          handlers.onChunk?.({ streamId, delta: 'Hello ' });
          handlers.onChunk?.({ streamId, delta: 'world!' });
          handlers.onChunk?.({
            streamId,
            finishReason: 'stop',
            usage: { promptTokens: 5, completionTokens: 3, totalTokens: 8 },
          });
          handlers.onDone?.({ streamId, ok: true });
        } else if (scenario === 'tool-call') {
          handlers.onChunk?.({ streamId, delta: 'using tool…' });
          handlers.onChunk?.({
            streamId,
            toolCall: {
              id: 'call_1',
              name: 'echo',
              arguments: { message: 'hi' },
            },
          });
          handlers.onDone?.({ streamId, ok: true });
        } else if (scenario === 'error') {
          handlers.onChunk?.({ streamId, delta: 'partial' });
          handlers.onError?.({
            streamId,
            kind: 'RuntimeError',
            message: 'upstream blew up',
          });
        } else if (scenario === 'cancel') {
          handlers.onChunk?.({ streamId, delta: 'partial' });
          handlers.onDone?.({ streamId, ok: false, cancelled: true });
        }
        // 'never-settles': deliberately emits no onDone/onError, simulating
        // a long-running generation that only a cancel can interrupt.
      });
      return streamId;
    },
  );
  return supervisor;
}

describe('SidecarLlmProvider', () => {
  it('aggregates chunks into a single LlmGenerateResult and forwards onToken deltas', async () => {
    const fake = createFakeSupervisor('happy');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });

    const tokens: string[] = [];
    const result = await provider.generateStream({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: 'hi' }],
      callbacks: { onToken: (t) => tokens.push(t) },
    });

    expect(tokens).toEqual(['Hello ', 'world!']);
    expect(result.content).toBe('Hello world!');
    expect(result.toolCalls).toEqual([]);
    expect(result.tokensUsed).toEqual({ prompt: 5, completion: 3, total: 8 });
    expect(fake.streamChat).toHaveBeenCalledTimes(1);
  });

  it('captures tool calls emitted by the sidecar', async () => {
    const fake = createFakeSupervisor('tool-call');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });

    const result = await provider.generate({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: 'do it' }],
      tools: [
        { name: 'echo', description: 'echoes', inputSchema: { type: 'object' } },
      ],
    });

    expect(result.content).toBe('using tool…');
    expect(result.toolCalls).toEqual([
      { id: 'call_1', name: 'echo', arguments: { message: 'hi' } },
    ]);
  });

  it('rejects the result promise when the sidecar emits stream.error', async () => {
    const fake = createFakeSupervisor('error');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });

    await expect(
      provider.generateStream({
        model: 'gpt-4o-mini',
        messages: [{ role: 'user', content: 'hi' }],
      }),
    ).rejects.toThrow(/sidecar chat stream failed: RuntimeError/);
  });

  it('treats cancelled streams as a successful but truncated result', async () => {
    const fake = createFakeSupervisor('cancel');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });

    const result = await provider.generateStream({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: 'hi' }],
    });

    // The provider doesn't surface ``cancelled`` in the LlmGenerateResult shape;
    // the partial content is whatever was streamed before cancel landed.
    expect(result.content).toBe('partial');
  });

  it('forwards provider-specific config (baseUrl/apiKey/temperature) to the sidecar request', async () => {
    const fake = createFakeSupervisor('happy');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'anthropic',
      model: 'claude-3-5-sonnet',
      baseUrl: 'https://api.anthropic.com',
      apiKey: 'sk-ant-xxx',
      temperature: 0.42,
    });

    await provider.generate({
      model: 'claude-3-5-sonnet',
      messages: [{ role: 'user', content: 'hi' }],
    });

    expect(fake.streamChat).toHaveBeenCalledTimes(1);
    const [request] = fake.streamChat.mock.calls[0] as [SidecarChatStreamRequest, unknown];
    expect(request.provider).toBe('anthropic');
    expect(request.baseUrl).toBe('https://api.anthropic.com');
    expect(request.apiKey).toBe('sk-ant-xxx');
    expect(request.temperature).toBe(0.42);
    expect(request.model).toBe('claude-3-5-sonnet');
  });

  it('rejects immediately without calling the sidecar when the signal is already aborted', async () => {
    const fake = createFakeSupervisor('happy');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });
    const controller = new AbortController();
    controller.abort();

    await expect(
      provider.generateStream({
        model: 'gpt-4o-mini',
        messages: [{ role: 'user', content: 'hi' }],
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(fake.streamChat).not.toHaveBeenCalled();
  });

  it('cancels the in-flight sidecar stream and rejects when aborted mid-generation', async () => {
    const fake = createFakeSupervisor('never-settles');
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
    });
    const controller = new AbortController();

    const resultPromise = provider.generateStream({
      model: 'gpt-4o-mini',
      messages: [{ role: 'user', content: 'hi' }],
      signal: controller.signal,
    });

    // Give streamChat's queueMicrotask handlers a chance to run before aborting.
    await new Promise((resolve) => setTimeout(resolve, 0));
    controller.abort();

    await expect(resultPromise).rejects.toMatchObject({ name: 'AbortError' });
    expect(fake.cancelChat).toHaveBeenCalledWith('str_test');
  });

  it('listModels returns the gateway catalog ids from the sidecar', async () => {
    const fake = {
      ...createFakeSupervisor('happy'),
      listModels: vi.fn(async () => ({
        models: [
          { id: 'deepseek-v4-flash' },
          { id: 'qwen3.8-27b' },
        ],
        catalogStatus: 'live' as const,
        current: { model: null, reasoningEffort: null },
      })),
    };
    const provider = new SidecarLlmProvider({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      supervisor: fake as any,
      provider: 'openai_compat',
      model: 'gpt-4o-mini',
      baseUrl: 'https://gateway.example.com/v1',
      apiKey: 'sk-test',
    });

    expect(await provider.listModels()).toEqual(['deepseek-v4-flash', 'qwen3.8-27b']);
    // 凭证随调用显式下发——sidecar 进程 env 不携带应用设置。
    expect(fake.listModels).toHaveBeenCalledWith({
      baseUrl: 'https://gateway.example.com/v1',
      apiKey: 'sk-test',
      provider: 'openai_compat',
    });
  });

  it('listModels falls back to the configured model when the catalog is empty or unreachable', async () => {
    const emptyCatalog = {
      ...createFakeSupervisor('happy'),
      listModels: vi.fn(async () => ({
        models: [],
        catalogStatus: 'offline' as const,
        error: 'connect refused',
        current: { model: null, reasoningEffort: null },
      })),
    };
    const unreachable = {
      ...createFakeSupervisor('happy'),
      listModels: vi.fn(async () => {
        throw new Error('sidecar timeout');
      }),
    };
    for (const fake of [emptyCatalog, unreachable]) {
      const provider = new SidecarLlmProvider({
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        supervisor: fake as any,
        provider: 'openai_compat',
        model: 'gpt-4o-mini',
      });
      expect(await provider.listModels()).toEqual(['gpt-4o-mini']);
    }
  });
});
