/**
 * chat-transport：Electron IPC SSE → 框架 ChatStreamTransport 的适配。
 * 分两层锁定：
 *   - LocalBackendSseAdapter（经 __test__ 导出）：legacy 帧归一化、
 *     budget_exhausted 降级护栏（已有内容流过时降级为普通结束，避免
 *     框架 hook 把已渲染文本冲掉）、done 恰好一次、turn_timeline 旁路。
 *   - createElectronChatTransport：请求形状（路径编码、metadata 并入
 *     body）、错误路径（startStream 拒绝 / error 载荷都会 reject 并
 *     发 error 事件）、cancelActive 双路径（有 streamId 走 cancelStream，
 *     否则按 chatId 打取消端点）、steer 的软失败。
 * 桥走真实的 window.electron 路径，不 mock 模块。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import {
  createElectronChatTransport,
  regenerateChatMessage,
  __test__,
} from './chat-transport';

const { LocalBackendSseAdapter } = __test__;

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
});

function collectEvents(): { events: SSEEvent[]; onEvent: (e: SSEEvent) => void } {
  const events: SSEEvent[] = [];
  return { events, onEvent: (e) => events.push(e) };
}

function ofType<T extends SSEEvent['type']>(events: SSEEvent[], type: T) {
  return events.filter((e) => e.type === type);
}

describe('LocalBackendSseAdapter 帧归一化', () => {
  it('content 帧按序透出，并附带 turn_timeline 旁路事件', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"content":"你好"}\n\n');
    adapter.feed('data: {"content":"世界"}\n\n');
    const contents = ofType(events, 'content').map((e) => (e as { content: string }).content);
    expect(contents).toEqual(['你好', '世界']);
    const timelines = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'turn_timeline',
    );
    expect(timelines.length).toBe(2);
    const last = timelines[1] as unknown as {
      payload: { blocks: Array<{ type: string; content: string }> };
    };
    expect(last.payload.blocks).toEqual([{ type: 'text', content: '你好世界' }]);
  });

  it('reasoning 帧写入 turn_timeline', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"type":"reasoning","content":"想一下"}\n\n');
    adapter.feed('data: {"type":"reasoning","content":"再决定"}\n\n');
    const timelines = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'turn_timeline',
    );
    expect(timelines.length).toBe(2);
    const last = timelines[1] as unknown as {
      payload: { blocks: Array<{ type: string; content: string }> };
    };
    expect(last.payload.blocks).toEqual([{ type: 'reasoning', content: '想一下再决定' }]);
  });

  it('[DONE] 帧触发一次 done；之后再 end 不重复', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"content":"x"}\n\ndata: [DONE]\n\n');
    adapter.end();
    expect(ofType(events, 'done')).toHaveLength(1);
  });

  it('没有 [DONE] 时 end() 补发一次 done', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"content":"x"}\n\n');
    adapter.end();
    expect(ofType(events, 'done')).toHaveLength(1);
  });

  // fixture 与服务端 router.ts onNotice 的真实发射形状一致（budget.kind + message）。
  it('budget_exhausted 在没有任何内容流过时原样透出、不结束流', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed(
      'data: {"type":"budget_exhausted","budget":{"kind":"tokens"},"message":"budget_exhausted: tokens"}\n\n',
    );
    expect(ofType(events, 'budget_exhausted')).toHaveLength(1);
    expect(ofType(events, 'done')).toHaveLength(0);
  });

  it('budget_exhausted 在已有内容流过时降级为普通结束（护栏）', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"content":"半截回答"}\n\n');
    adapter.feed(
      'data: {"type":"budget_exhausted","budget":{"kind":"tokens"},"message":"budget_exhausted: tokens"}\n\n',
    );
    // 原事件被吞，替换为 suppression 标记 + done。
    expect(ofType(events, 'budget_exhausted')).toHaveLength(0);
    const suppressed = events.filter(
      (e) =>
        e.type === 'agent' &&
        (e as { event?: string }).event === 'budget_exhausted_suppressed',
    );
    expect(suppressed).toHaveLength(1);
    expect(ofType(events, 'done')).toHaveLength(1);
  });

  it('round_end 封住当前思考段，后续 reasoning 另起一块', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"type":"reasoning","content":"第一轮"}\n\n');
    adapter.feed('data: {"type":"completion","status":"executing"}\n\n');
    adapter.feed('data: {"type":"reasoning","content":"第二轮"}\n\n');
    const timelines = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'turn_timeline',
    );
    const last = timelines[timelines.length - 1] as unknown as {
      payload: { blocks: Array<{ type: string; content: string }> };
    };
    expect(last.payload.blocks.map((b) => b.content)).toEqual(['第一轮', '第二轮']);
  });

  it('llm_speed 统计思考和回复，round_end 冻结当前请求', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed('data: {"type":"reasoning","content":"先读配置先读配置先读配置先读配置"}\n\n');
    adapter.feed('data: {"content":"问好完成。"}\n\n');
    const speeds = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'llm_speed',
    ) as Array<{ payload: { tokens: number; live: boolean } }>;
    expect(speeds.length).toBeGreaterThanOrEqual(2);
    expect(speeds[speeds.length - 1].payload.tokens).toBe(13);
    expect(speeds[speeds.length - 1].payload.live).toBe(true);
    adapter.feed('data: {"type":"completion","status":"executing"}\n\n');
    const afterRound = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'llm_speed',
    ) as Array<{ payload: { live: boolean } }>;
    expect(afterRound[afterRound.length - 1].payload.live).toBe(false);
  });

  it('executed_actions 帧透出并同步进时间线', () => {
    const { events, onEvent } = collectEvents();
    const adapter = new LocalBackendSseAdapter(onEvent);
    adapter.feed(
      'data: {"type":"executed_actions","actions":[{"id":"t1","toolName":"bash","status":"done"}]}\n\n',
    );
    const executed = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'executed_actions',
    );
    expect(executed).toHaveLength(1);
    const timelines = events.filter(
      (e) => e.type === 'agent' && (e as { event?: string }).event === 'turn_timeline',
    );
    expect(timelines).toHaveLength(1);
  });
});

describe('createElectronChatTransport', () => {
  function installStreamBridge() {
    const captured: {
      input?: { method: string; path: string; body?: unknown };
      cb?: (payload: { type: string; chunk?: string; status?: number; error?: string }) => void;
    } = {};
    const cancelStream = vi.fn();
    const request = vi.fn().mockResolvedValue({});
    const startStream = vi.fn(
      (
        input: { method: string; path: string; body?: unknown },
        cb: (payload: { type: string; chunk?: string; status?: number; error?: string }) => void,
      ) => {
        captured.input = input;
        captured.cb = cb;
        return Promise.resolve('stream-1');
      },
    );
    (window as { electron?: unknown }).electron = {
      localBackend: { request, startStream, cancelStream },
    };
    return { captured, cancelStream, request, startStream };
  }

  it('桥缺失时 stream 直接拒绝', async () => {
    const transport = createElectronChatTransport('chat-1');
    await expect(
      transport.stream({ content: 'hi' }, () => {}),
    ).rejects.toThrow(/Electron bridge unavailable/);
  });

  it('请求形状：路径编码 chatId，metadata 并入 body', async () => {
    const { captured } = installStreamBridge();
    const transport = createElectronChatTransport('chat 1');
    const { onEvent } = collectEvents();
    const p = transport.stream({ content: 'hi', metadata: { foo: 1 } }, onEvent);
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    expect(captured.input).toEqual({
      method: 'POST',
      path: '/api/v2/chats/chat%201/run',
      body: { message: 'hi', foo: 1 },
    });
    captured.cb!({ type: 'end', status: 200 });
    await p;
  });

  it('data → end 载荷驱动适配器，end 后 stream resolve 且返回取消句柄', async () => {
    const { captured, cancelStream } = installStreamBridge();
    const transport = createElectronChatTransport('chat-1');
    const { events, onEvent } = collectEvents();
    const p = transport.stream({ content: 'hi' }, onEvent);
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    captured.cb!({ type: 'data', chunk: 'data: {"content":"x"}\n\n' });
    captured.cb!({ type: 'end', status: 200 });
    const cancel = await p;
    expect(ofType(events, 'content')).toHaveLength(1);
    expect(ofType(events, 'done')).toHaveLength(1);
    // 流已结束，契约句柄此时取消仍指向记忆中的 streamId（空操作语义见文件头）。
    if (typeof cancel !== 'function') throw new Error('expected a cancel handle');
    cancel();
    expect(cancelStream).toHaveBeenCalledWith('stream-1');
  });

  it('startStream 拒绝时发 error 事件并 reject', async () => {
    const { startStream } = installStreamBridge();
    startStream.mockRejectedValueOnce(new Error('ipc dead'));
    const transport = createElectronChatTransport('chat-1');
    const { events, onEvent } = collectEvents();
    await expect(transport.stream({ content: 'hi' }, onEvent)).rejects.toThrow('ipc dead');
    expect(ofType(events, 'error')).toHaveLength(1);
  });

  it('error 载荷发 error 事件并 reject', async () => {
    const { captured } = installStreamBridge();
    const transport = createElectronChatTransport('chat-1');
    const { events, onEvent } = collectEvents();
    const p = transport.stream({ content: 'hi' }, onEvent);
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    captured.cb!({ type: 'error', error: 'agent crashed' });
    await expect(p).rejects.toThrow('agent crashed');
    expect(ofType(events, 'error')).toEqual([{ type: 'error', message: 'agent crashed' }]);
  });

  it('cancelActive 有进行中流时走 cancelStream，否则按 chatId 打取消端点', async () => {
    const { captured, cancelStream, request } = installStreamBridge();
    const transport = createElectronChatTransport('chat-1');
    const { onEvent } = collectEvents();
    const p = transport.stream({ content: 'hi' }, onEvent);
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    // startStream 已 resolve，streamId 已登记——取消走 IPC。
    transport.cancelActive();
    expect(cancelStream).toHaveBeenCalledWith('stream-1');
    captured.cb!({ type: 'end', status: 200 });
    await p;
    // 流已结束后再取消：改打 HTTP 取消端点（fire-and-forget）。
    transport.cancelActive();
    expect(request).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/v2/chats/chat-1/cancel',
      body: {},
    });
  });

  it('steer 在桥无 steerChat 能力时软失败为 false', async () => {
    installStreamBridge();
    const transport = createElectronChatTransport('chat-1');
    await expect(transport.steer!('换个方向')).resolves.toBe(false);
  });

  it('steer 透传桥的返回值', async () => {
    const steerChat = vi.fn().mockResolvedValue(true);
    (window as { electron?: unknown }).electron = { localBackend: { steerChat } };
    const transport = createElectronChatTransport('chat-1');
    await expect(transport.steer!('换个方向')).resolves.toBe(true);
    expect(steerChat).toHaveBeenCalledWith('chat-1', '换个方向');
  });
});

describe('regenerateChatMessage', () => {
  function installRegenerateBridge() {
    const captured: {
      cb?: (payload: { type: string; chunk?: string; status?: number; error?: string }) => void;
    } = {};
    const startStream = vi.fn(
      (
        _input: unknown,
        cb: (payload: { type: string; chunk?: string; status?: number; error?: string }) => void,
      ) => {
        captured.cb = cb;
        return Promise.resolve('stream-r');
      },
    );
    (window as { electron?: unknown }).electron = { localBackend: { startStream } };
    return captured;
  }

  it('2xx 结束 → resolve', async () => {
    const captured = installRegenerateBridge();
    const p = regenerateChatMessage('chat-1', 'msg-1');
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    captured.cb!({ type: 'end', status: 200 });
    await expect(p).resolves.toBeUndefined();
  });

  it('error 帧 + 非 2xx 结束 → 以后端的拒绝文案 reject', async () => {
    const captured = installRegenerateBridge();
    const p = regenerateChatMessage('chat-1', 'msg-1');
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    captured.cb!({ type: 'data', chunk: 'event: error\ndata: {"message":"该回复不可重新生成"}\n\n' });
    captured.cb!({ type: 'end', status: 400 });
    await expect(p).rejects.toThrow('该回复不可重新生成');
  });

  it('非 2xx 且无 error 帧 → 带状态码的兜底文案', async () => {
    const captured = installRegenerateBridge();
    const p = regenerateChatMessage('chat-1', 'msg-1');
    await vi.waitFor(() => expect(captured.cb).toBeDefined());
    captured.cb!({ type: 'end', status: 500 });
    await expect(p).rejects.toThrow('重新生成失败（HTTP 500）');
  });

  it('桥缺失时拒绝', async () => {
    await expect(regenerateChatMessage('chat-1', 'msg-1')).rejects.toThrow(
      /Electron bridge unavailable/,
    );
  });
});
