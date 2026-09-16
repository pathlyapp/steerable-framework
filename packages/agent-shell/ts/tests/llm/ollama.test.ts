import { afterEach, describe, expect, it, vi } from 'vitest';
import { OllamaProvider } from '../../src/llm/ollama';

function jsonResponse(body: unknown, ok = true): Response {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

describe('OllamaProvider', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('generate() passes the request signal through to fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ message: { content: 'hi' } }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OllamaProvider({ model: 'llama3.1:8b' });
    const controller = new AbortController();
    await provider.generate({
      model: 'llama3.1:8b',
      messages: [{ role: 'user', content: 'hi' }],
      signal: controller.signal,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  it('generate() rethrows the original AbortError instead of the generic "无法连接" message', async () => {
    const controller = new AbortController();
    const abortError = new DOMException('The operation was aborted.', 'AbortError');
    const fetchMock = vi.fn().mockImplementation(() => {
      controller.abort();
      return Promise.reject(abortError);
    });
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OllamaProvider({ model: 'llama3.1:8b' });

    await expect(
      provider.generate({
        model: 'llama3.1:8b',
        messages: [{ role: 'user', content: 'hi' }],
        signal: controller.signal,
      }),
    ).rejects.toBe(abortError);
  });

  it('generateStream() passes the request signal through to fetch', async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      start(streamController) {
        streamController.enqueue(
          encoder.encode(`${JSON.stringify({ message: { content: 'hi' }, done: true })}\n`),
        );
        streamController.close();
      },
    });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OllamaProvider({ model: 'llama3.1:8b' });
    const controller = new AbortController();
    await provider.generateStream({
      model: 'llama3.1:8b',
      messages: [{ role: 'user', content: 'hi' }],
      signal: controller.signal,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  it('generate() still wraps genuine network failures (signal not aborted) into a friendly error', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('fetch failed'));
    vi.stubGlobal('fetch', fetchMock);

    const provider = new OllamaProvider({ model: 'llama3.1:8b' });

    await expect(
      provider.generate({
        model: 'llama3.1:8b',
        messages: [{ role: 'user', content: 'hi' }],
      }),
    ).rejects.toThrow(/无法连接本地 Ollama/);
  });
});
