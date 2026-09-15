import { afterEach, describe, expect, it, vi } from 'vitest';
import { OpenAICompatProvider } from '../../src/llm/openai-compat';
import { resetForcedToolChoiceCompatForTests } from '../../src/llm/tool-choice';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  } as unknown as Response;
}

describe('OpenAICompatProvider.generate()', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetForcedToolChoiceCompatForTests();
  });

  it('passes the request signal through to fetch so aborting tears down the HTTP request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ choices: [{ message: { content: 'hi' } }] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-test',
    });
    const controller = new AbortController();
    await provider.generate({
      model: 'gpt-test',
      messages: [{ role: 'user', content: 'hi' }],
      signal: controller.signal,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  it('rethrows the original AbortError instead of wrapping it in a "无法连接" message', async () => {
    const controller = new AbortController();
    const abortError = new DOMException('The operation was aborted.', 'AbortError');
    const fetchMock = vi.fn().mockImplementation(() => {
      controller.abort();
      return Promise.reject(abortError);
    });
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-test',
    });

    await expect(
      provider.generate({
        model: 'gpt-test',
        messages: [{ role: 'user', content: 'hi' }],
        signal: controller.signal,
      }),
    ).rejects.toBe(abortError);
  });

  it('still wraps genuine network failures (signal not aborted) into a friendly connection error', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-test',
    });

    await expect(
      provider.generate({
        model: 'gpt-test',
        messages: [{ role: 'user', content: 'hi' }],
      }),
    ).rejects.toThrow(/无法连接 OpenAI 兼容网关/);
  });

  it('W6-3: user messages with images serialize to the vision content-array form', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ choices: [{ message: { content: 'ok' } }] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-test',
    });
    await provider.generate({
      model: 'gpt-test',
      messages: [
        {
          role: 'user',
          content: '看图说话',
          images: [{ data: 'QUJD', mediaType: 'image/png' }],
        },
      ],
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as {
      messages: Array<{ role: string; content: unknown }>;
    };
    expect(body.messages[0].content).toEqual([
      { type: 'text', text: '看图说话' },
      { type: 'image_url', image_url: { url: 'data:image/png;base64,QUJD' } },
    ]);
  });

  it('W6-3: text-only user messages keep the plain string content shorthand', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ choices: [{ message: { content: 'ok' } }] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-test',
    });
    await provider.generate({
      model: 'gpt-test',
      messages: [{ role: 'user', content: 'hi' }],
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as {
      messages: Array<{ role: string; content: unknown }>;
    };
    expect(body.messages[0].content).toBe('hi');
  });
});

describe('OpenAICompatProvider.generate() tool_choice wire format', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetForcedToolChoiceCompatForTests();
  });

  const sampleTools = [
    { name: 'list_workspaces', description: 'List workspaces' },
  ];

  async function capturePayload(opts: {
    model: string;
    toolChoice?: 'auto' | 'none' | 'required';
  }): Promise<Record<string, unknown>> {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ choices: [{ message: { content: 'ok' } }] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: opts.model,
    });
    await provider.generate({
      model: opts.model,
      messages: [{ role: 'user', content: '列出当前工区' }],
      tools: sampleTools,
      toolChoice: opts.toolChoice,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    return JSON.parse(String(init.body)) as Record<string, unknown>;
  }

  it('rewrites required → auto when the model id advertises thinking/reasoner', async () => {
    const payload = await capturePayload({
      model: 'some-vendor-reasoner',
      toolChoice: 'required',
    });
    expect(payload.tool_choice).toBe('auto');
    expect(payload.tools).toEqual(expect.any(Array));
  });

  it('still sends tool_choice=required for ordinary chat models', async () => {
    const payload = await capturePayload({
      model: 'deepseek-chat',
      toolChoice: 'required',
    });
    expect(payload.tool_choice).toBe('required');
  });

  it('retries with auto when a 400 says forced tool_choice is unsupported', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error: {
              message: 'Thinking mode does not support this tool_choice',
              type: 'invalid_request_error',
            },
          },
          400,
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ choices: [{ message: { content: 'ok' } }] }));
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'deepseek-v4-flash',
    });
    const result = await provider.generate({
      model: 'deepseek-v4-flash',
      messages: [{ role: 'user', content: '列出当前工区' }],
      tools: sampleTools,
      toolChoice: 'required',
    });

    expect(result.content).toBe('ok');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const first = JSON.parse(String((fetchMock.mock.calls[0] as [string, RequestInit])[1].body));
    const second = JSON.parse(String((fetchMock.mock.calls[1] as [string, RequestInit])[1].body));
    expect(first.tool_choice).toBe('required');
    expect(second.tool_choice).toBe('auto');
  });

  it('remembers the 400 and does not send required on the next call', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        jsonResponse({ error: { message: 'does not support this tool_choice' } }, 400),
      )
      .mockResolvedValue(jsonResponse({ choices: [{ message: { content: 'ok' } }] }));
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gateway-custom-v4',
    });
    const req = {
      model: 'gateway-custom-v4',
      messages: [{ role: 'user' as const, content: 'hi' }],
      tools: sampleTools,
      toolChoice: 'required' as const,
    };
    await provider.generate(req);
    await provider.generate(req);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    const third = JSON.parse(String((fetchMock.mock.calls[2] as [string, RequestInit])[1].body));
    expect(third.tool_choice).toBe('auto');
  });

  it('does not retry unrelated 400s that mention tool messages', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          error: {
            message: "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'",
          },
        },
        400,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OpenAICompatProvider({
      baseUrl: 'https://api.example.com',
      model: 'gpt-4o',
    });

    await expect(
      provider.generate({
        model: 'gpt-4o',
        messages: [{ role: 'user', content: 'hi' }],
        tools: sampleTools,
        toolChoice: 'required',
      }),
    ).rejects.toThrow(/OpenAI 兼容请求失败: 400/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
