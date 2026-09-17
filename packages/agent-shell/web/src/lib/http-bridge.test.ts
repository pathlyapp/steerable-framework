/**
 * http-bridge：BS 模式下与 Electron preload 同形的宿主桥，传输换成 HTTP。
 * 锁定：request 的方法 / 头 / body 形状与错误语义（detail 优先、status 挂上）、
 * steer / approval / askUser / attachments 的端点逐字形状、startStream 的
 * data→end 事件序列与取消语义（本地取消按正常结束上报）、
 * 浏览器形态下 local.* 的降级回答（目录选择取消、截图不支持）。
 * fetch 用 vi.stubGlobal 替身，不起真实服务器。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createHttpBridge } from './http-bridge';
import type { LocalBackendStreamEvent } from './electron-bridge';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(handler: (path: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) =>
    Promise.resolve(handler(String(input), init)),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  };
}

/** 逐 chunk 产出字符串的可读流替身（body.getReader 的最小实现）。 */
function streamResponse(chunks: string[], status = 200) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: status >= 200 && status < 300,
    status,
    body: {
      getReader: () => ({
        read: () =>
          Promise.resolve(
            i < chunks.length
              ? { done: false as const, value: encoder.encode(chunks[i++]) }
              : { done: true as const, value: undefined },
          ),
      }),
    },
  };
}

describe('localBackend.request', () => {
  it('GET 不带 body 时不设 Content-Type、不带 body', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ ok: 1 }));
    const bridge = createHttpBridge();
    await bridge.localBackend.request({ method: 'GET', path: '/api/v2/chats' });
    expect(fetchMock).toHaveBeenCalledWith('/api/v2/chats', {
      method: 'GET',
      headers: {},
      body: undefined,
    });
  });

  it('POST 带 body 时加 JSON 头并序列化', async () => {
    const fetchMock = stubFetch(() => jsonResponse({}));
    const bridge = createHttpBridge();
    await bridge.localBackend.request({
      method: 'POST',
      path: '/api/v2/chats/new',
      body: { agentId: 'a1' },
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/v2/chats/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"agentId":"a1"}',
    });
  });

  it('非 2xx 且响应带 detail 时抛 detail，status 挂在错误上', async () => {
    stubFetch(() => jsonResponse({ detail: '参数错误' }, 422));
    const bridge = createHttpBridge();
    const err = await bridge.localBackend
      .request({ method: 'GET', path: '/x' })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).message).toBe('参数错误');
    expect((err as Error & { status?: number }).status).toBe(422);
  });

  it('非 2xx 且响应非 JSON 时退化为状态码描述', async () => {
    stubFetch(() => ({
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('not json')),
    }));
    const bridge = createHttpBridge();
    await expect(
      bridge.localBackend.request({ method: 'GET', path: '/x' }),
    ).rejects.toThrow('Request failed (500)');
  });
});

describe('host 端点形状', () => {
  it('steerChat 只在 ok===true 时为真', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ ok: true }));
    const bridge = createHttpBridge();
    await expect(bridge.localBackend.steerChat!('c1', '停一下')).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/steer',
      expect.objectContaining({ method: 'POST', body: '{"chatId":"c1","content":"停一下"}' }),
    );
  });

  it('approval.decide 与 askUser 请求打到各自端点', async () => {
    const fetchMock = stubFetch((input) =>
      String(input).endsWith('/pending') ? jsonResponse([]) : jsonResponse({}),
    );
    const bridge = createHttpBridge();
    await bridge.approval!.decide({ requestId: 'r1', kind: 'allow_once' });
    await expect(bridge.approval!.pending()).resolves.toEqual([]);
    await bridge.askUser!.answer({ requestId: 'r2', answers: { q: 'a' } });
    await expect(bridge.askUser!.pending()).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/approval/decide',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/approval/pending',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/ask-user/answer',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/ask-user/pending',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('attachments.save 透传到落盘端点', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ files: [] }));
    const bridge = createHttpBridge();
    await bridge.attachments!.save({ chatId: 'c1', files: [{ name: 'a.txt', data: 'SGk=' }] });
    expect(fetchMock).toHaveBeenCalledWith(
      '/host/attachments/save',
      expect.objectContaining({
        method: 'POST',
        body: '{"chatId":"c1","files":[{"name":"a.txt","data":"SGk="}]}',
      }),
    );
  });
});

describe('startStream / cancelStream', () => {
  it('按 chunk 发 data 事件，流尽发 end（带状态码），返回 streamId', async () => {
    stubFetch(() => streamResponse(['data: {"content":"a"}\n\n', 'data: [DONE]\n\n']));
    const bridge = createHttpBridge();
    const events: LocalBackendStreamEvent[] = [];
    const streamId = await bridge.localBackend.startStream(
      { method: 'POST', path: '/api/v2/chats/c1/run', body: { message: 'hi' } },
      (e) => events.push(e),
    );
    expect(typeof streamId).toBe('string');
    await vi.waitFor(() => {
      expect(events).toEqual([
        { type: 'data', chunk: 'data: {"content":"a"}\n\n' },
        { type: 'data', chunk: 'data: [DONE]\n\n' },
        { type: 'end', status: 200 },
      ]);
    });
  });

  it('fetch 本身失败时发 error 事件并返回 null', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('connection refused'));
    vi.stubGlobal('fetch', fetchMock);
    const bridge = createHttpBridge();
    const events: LocalBackendStreamEvent[] = [];
    const streamId = await bridge.localBackend.startStream(
      { method: 'POST', path: '/x' },
      (e) => events.push(e),
    );
    expect(streamId).toBeNull();
    expect(events).toEqual([{ type: 'error', error: 'connection refused' }]);
  });

  it('非 2xx 响应发 error 事件并返回 null', async () => {
    stubFetch(() => ({ ok: false, status: 503, body: null }));
    const bridge = createHttpBridge();
    const events: LocalBackendStreamEvent[] = [];
    const streamId = await bridge.localBackend.startStream(
      { method: 'POST', path: '/x' },
      (e) => events.push(e),
    );
    expect(streamId).toBeNull();
    expect(events).toEqual([{ type: 'error', error: 'start stream failed (503)' }]);
  });

  it('取消进行中的流按正常结束（end 200）上报', async () => {
    const encoder = new TextEncoder();
    let rejectRead: ((err: Error) => void) | null = null;
    let readCount = 0;
    stubFetch(() => ({
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: () => {
            readCount += 1;
            if (readCount === 1) {
              return Promise.resolve({ done: false, value: encoder.encode('chunk-1') });
            }
            return new Promise((_, reject) => {
              rejectRead = reject;
            });
          },
        }),
      },
    }));
    const bridge = createHttpBridge();
    const events: LocalBackendStreamEvent[] = [];
    const streamId = await bridge.localBackend.startStream(
      { method: 'POST', path: '/x' },
      (e) => events.push(e),
    );
    expect(streamId).not.toBeNull();
    await vi.waitFor(() => expect(events).toHaveLength(1));
    bridge.localBackend.cancelStream(streamId!);
    rejectRead!(new Error('The operation was aborted'));
    await vi.waitFor(() => {
      expect(events).toEqual([
        { type: 'data', chunk: 'chunk-1' },
        { type: 'end', status: 200 },
      ]);
    });
    // 重复取消是 no-op，不抛错。
    expect(() => bridge.localBackend.cancelStream(streamId!)).not.toThrow();
  });
});

describe('BS token（bootstrap 注入的 Bearer）', () => {
  const BOOT = { platform: 'darwin', flavor: 'test', brandName: 'T', token: 'tok 1' };

  it('request 与 startStream 带 Authorization 头', async () => {
    vi.stubGlobal('__DEEPPATH_BS__', BOOT);
    const fetchMock = stubFetch(() => jsonResponse({}));
    const bridge = createHttpBridge();
    await bridge.localBackend.request({ method: 'GET', path: '/api/v2/chats' });
    expect(fetchMock).toHaveBeenCalledWith('/api/v2/chats', {
      method: 'GET',
      headers: { Authorization: 'Bearer tok 1' },
      body: undefined,
    });
    await bridge.localBackend.startStream({ method: 'POST', path: '/x' }, () => {});
    expect(fetchMock).toHaveBeenCalledWith('/x', expect.objectContaining({
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer tok 1' },
    }));
  });

  it('EventSource 不能设头，token 走 query（URL 编码）', () => {
    vi.stubGlobal('__DEEPPATH_BS__', BOOT);
    const urls: string[] = [];
    vi.stubGlobal(
      'EventSource',
      class {
        constructor(url: string) {
          urls.push(url);
        }
        addEventListener(): void {}
      },
    );
    const bridge = createHttpBridge();
    bridge.onChatCreated?.(() => {});
    expect(urls).toEqual(['/api/v2/events?token=tok%201']);
  });
});

describe('浏览器形态的 local.* 降级', () => {
  it('目录选择直接取消（浏览器给不了路径）', async () => {
    stubFetch(() => jsonResponse({}));
    const bridge = createHttpBridge();
    await expect(bridge.local!.selectDirectory()).resolves.toEqual({
      canceled: true,
      filePaths: [],
    });
  });

  it('窗口截图明确不支持', async () => {
    stubFetch(() => jsonResponse({}));
    const bridge = createHttpBridge();
    const res = await bridge.local!.captureScreenshot();
    expect(res.success).toBe(false);
  });
});
