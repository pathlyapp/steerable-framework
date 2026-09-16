import { describe, expect, it, vi } from 'vitest';
import {
  buildWorldState,
  streamCoreLoopTurn,
} from '../../src/local-backend/coreloop-stream.js';
import type {
  SidecarChatStreamHandlers,
  SidecarChatStreamRequest,
  SidecarSupervisor,
} from '../../src/sidecar/index.js';

type Emitted = { method: 'chunk' | 'done' | 'error' | 'child'; payload: unknown };

/** A supervisor stub that plays a scripted notification sequence. */
function makeSupervisor(script: Emitted[]): {
  supervisor: SidecarSupervisor;
  sent: SidecarChatStreamRequest[];
} {
  const sent: SidecarChatStreamRequest[] = [];
  const supervisor = {
    streamChat: vi.fn(
      async (request: SidecarChatStreamRequest, handlers: SidecarChatStreamHandlers) => {
        sent.push(request);
        for (const item of script) {
          if (item.method === 'chunk') handlers.onChunk?.(item.payload as never);
          if (item.method === 'done') handlers.onDone?.(item.payload as never);
          if (item.method === 'error') handlers.onError?.(item.payload as never);
          if (item.method === 'child') handlers.onChildEvent?.(item.payload as never);
        }
        return 'stream-1';
      },
    ),
    cancelChat: vi.fn(async () => {}),
  } as unknown as SidecarSupervisor;
  return { supervisor, sent };
}

const BASE_OPTIONS = {
  chatId: 'chat-1',
  messages: [{ role: 'user', content: 'hi' }] as never[],
  tools: [
    { name: 'local_read_file', description: 'read', inputSchema: { type: 'object' } },
  ],
  provider: 'ollama',
  model: 'llama3.1:8b',
};

describe('streamCoreLoopTurn', () => {
  it('forwards a per-turn reasoningEffort verbatim, and omits it when unset', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      reasoningEffort: 'high',
      onText: () => {},
    });
    expect(sent[0].reasoningEffort).toBe('high');

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });
    expect(sent[1].reasoningEffort).toBeUndefined();
  });

  it('sends useCoreLoop + toolsViaHost with fc-shaped tools', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    expect(sent).toHaveLength(1);
    const req = sent[0];
    expect(req.useCoreLoop).toBe(true);
    expect(req.contentMode).toBe('all');
    expect(req.toolsViaHost).toBe(true);
    expect(req.chatId).toBe('chat-1');
    expect(req.tools).toEqual([
      {
        type: 'function',
        function: {
          name: 'local_read_file',
          description: 'read',
          parameters: { type: 'object' },
        },
      },
    ]);
  });

  it('W6-3: a message carrying images is sent as structured wire parts', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      messages: [
        {
          role: 'user',
          content: '看图',
          images: [{ data: 'QUJD', mediaType: 'image/png' }],
        },
      ] as never[],
    });

    const msg = sent[0].messages[0] as {
      role: string;
      content: string;
      parts?: unknown[];
    };
    expect(msg.parts).toEqual([
      { type: 'text', text: '看图' },
      { type: 'image', data: 'QUJD', mediaType: 'image/png' },
    ]);
  });

  it('W6-3: text-only messages do not gain a parts field', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    const msg = sent[0].messages[0] as { parts?: unknown[] };
    expect(msg.parts).toBeUndefined();
  });

  it('W7-1: forwards resume:true with an empty message list (the sidecar replays the record)', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      resume: true,
      messages: [] as never[],
      recordId: 'chat-1',
    });

    expect(sent).toHaveLength(1);
    expect(sent[0].resume).toBe(true);
    expect(sent[0].messages).toEqual([]);
    expect(sent[0].recordId).toBe('chat-1');
  });

  it('W7-1: omits resume on a normal turn', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    // Same convention as baseUrl/apiKey: the key may exist with value
    // undefined; JSON-RPC serialization drops it from the wire.
    expect(sent[0].resume).toBeUndefined();
  });

  it('forwards worldState sections to the sidecar', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      worldState: buildWorldState({ now: new Date(2026, 7, 29, 10, 20) }),
      onText: () => {},
    });

    const worldState = sent[0].worldState as {
      time: { local: string; timezone: string; weekday: string };
    };
    // Minute precision — no seconds, so back-to-back turns within the same
    // minute diff to a no-op on the sidecar.
    expect(worldState.time.local).toBe('2026-08-29T10:20');
    expect(worldState.time.timezone).toBeTruthy();
    expect(worldState.time.weekday).toBe('周六');
  });

  it('buildWorldState emits only the time section by default (W6-7b opt-in)', () => {
    const state = buildWorldState({ now: new Date(2026, 7, 29, 10, 20) });
    expect(Object.keys(state)).toEqual(['time']);
  });

  it('buildWorldState adds mode / permissions / skills sections when given (W6-7b)', () => {
    const state = buildWorldState({
      now: new Date(2026, 7, 29, 10, 20),
      mode: 'plan',
      permissions: {
        approval: 'host',
        sandbox: { enabled: true, writableRoots: ['/tmp/proj'], network: true },
      },
      skills: { conditions: ['plan-mode'], exclude: ['local-exec'] },
    }) as {
      mode: { name: string };
      permissions: {
        approval: string;
        sandbox: { enabled: boolean; writableRoots: string[]; network: boolean };
      };
      skills: { conditions: string[]; exclude: string[] };
    };

    expect(state.mode).toEqual({ name: 'plan' });
    expect(state.permissions).toEqual({
      approval: 'host',
      sandbox: { enabled: true, writableRoots: ['/tmp/proj'], network: true },
    });
    expect(state.skills).toEqual({ conditions: ['plan-mode'], exclude: ['local-exec'] });
  });

  it('omits worldState when not provided', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    expect(sent[0].worldState).toBeUndefined();
  });

  it('W6-10: forwards the full notice payload so hosts can detect a framework compaction', async () => {
    const { supervisor } = makeSupervisor([
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          notice: { kind: 'hook_action', hook: 'pre_step', action: 'compact', round: 0 },
        },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const onNotice = vi.fn();

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {}, onNotice });

    expect(onNotice).toHaveBeenCalledWith(
      'hook_action',
      expect.objectContaining({ action: 'compact' }),
    );
  });

  it('streams text deltas and resolves with the done status', async () => {
    const { supervisor } = makeSupervisor([
      { method: 'chunk', payload: { streamId: 'stream-1', delta: 'hello ' } },
      { method: 'chunk', payload: { streamId: 'stream-1', delta: 'world' } },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const onText = vi.fn();

    const outcome = await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText,
    });

    expect(onText.mock.calls.map((c) => c[0]).join('')).toBe('hello world');
    expect(outcome.status).toBe('completed');
  });

  it('forwards streamRawChunks and surfaces pre-digestion rawChunk payloads', async () => {
    const { supervisor, sent } = makeSupervisor([
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          rawChunk: { contentDelta: 'answer <function_results>{"fake": 1}</function_results>' },
        },
      },
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          rawChunk: { toolCallDelta: { id: 'c1', name: 'add', arguments: { a: 1 } } },
        },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const onRawChunk = vi.fn();

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      streamRawChunks: true,
      onRawChunk,
    });

    expect(sent[0].streamRawChunks).toBe(true);
    expect(onRawChunk.mock.calls.map((c) => c[0])).toEqual([
      { contentDelta: 'answer <function_results>{"fake": 1}</function_results>' },
      { toolCallDelta: { id: 'c1', name: 'add', arguments: { a: 1 } } },
    ]);
  });

  it('omits streamRawChunks by default', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    // Same convention as resume: the key may exist with value undefined;
    // JSON-RPC serialization drops it from the wire.
    expect(sent[0].streamRawChunks).toBeUndefined();
  });

  it('pairs tool results with their start arguments', async () => {
    const { supervisor } = makeSupervisor([
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          toolCall: { id: 'c1', name: 'local_read_file', arguments: { path: '/a' } },
        },
      },
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          toolResult: { id: 'c1', name: 'local_read_file', success: true, resultPreview: 'data', durationMs: 5 },
        },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const onToolAction = vi.fn();

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {}, onToolAction });

    expect(onToolAction).toHaveBeenCalledWith({
      id: 'c1',
      tool: 'local_read_file',
      arguments: { path: '/a' },
      success: true,
      result: 'data',
      error: undefined,
      durationMs: 5,
      sandbox: undefined,
    });
  });

  it('forwards reasoning deltas and tool-start before the result', async () => {
    const { supervisor } = makeSupervisor([
      { method: 'chunk', payload: { streamId: 'stream-1', reasoningDelta: '想想' } },
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          toolCall: { id: 'c1', name: 'local_read_file', arguments: { path: '/a' } },
        },
      },
      {
        method: 'chunk',
        payload: {
          streamId: 'stream-1',
          toolResult: { id: 'c1', name: 'local_read_file', success: true, resultPreview: 'data' },
        },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const onReasoning = vi.fn();
    const onToolStart = vi.fn();
    const onToolAction = vi.fn();

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      onReasoning,
      onToolStart,
      onToolAction,
    });

    expect(onReasoning).toHaveBeenCalledWith('想想');
    expect(onToolStart).toHaveBeenCalledWith({
      id: 'c1',
      tool: 'local_read_file',
      arguments: { path: '/a' },
    });
    expect(onToolStart.mock.invocationCallOrder[0]).toBeLessThan(
      onToolAction.mock.invocationCallOrder[0],
    );
  });

  it('forwards notices and rejects on stream.error', async () => {
    const { supervisor } = makeSupervisor([
      { method: 'chunk', payload: { streamId: 'stream-1', notice: { kind: 'soft_timeout' } } },
      { method: 'error', payload: { streamId: 'stream-1', kind: 'LoopError', message: 'boom' } },
    ]);
    const onNotice = vi.fn();

    await expect(
      streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {}, onNotice }),
    ).rejects.toThrow('coreloop stream failed: LoopError: boom');
    expect(onNotice).toHaveBeenCalledWith('soft_timeout', { kind: 'soft_timeout' });
  });

  it('cancels the sidecar stream on abort', async () => {
    vi.useFakeTimers();
    try {
      const { supervisor } = makeSupervisor([
        // never completes — abort is what settles it
      ]);
      // make streamChat hang until cancelled
      (supervisor.streamChat as ReturnType<typeof vi.fn>).mockImplementation(async () => {
        await new Promise((r) => setTimeout(r, 5));
        return 'stream-9';
      });

      const controller = new AbortController();
      const pending = streamCoreLoopTurn({
        ...BASE_OPTIONS,
        supervisor,
        signal: controller.signal,
        onText: () => {},
      });
      const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      // abort fires at t=10ms; the cancel grace (no done from the sidecar)
      // settles the promise with a bare AbortError.
      setTimeout(() => controller.abort(), 10);
      await vi.advanceTimersByTimeAsync(10);
      await vi.advanceTimersByTimeAsync(10_000);
      await assertion;
      expect(supervisor.cancelChat).toHaveBeenCalledWith('stream-9');
    } finally {
      vi.useRealTimers();
    }
  });

  it('resolves with cancelled status + traceId when the cancelled done arrives', async () => {
    const { supervisor } = makeSupervisor([]);
    let handlersRef: SidecarChatStreamHandlers | undefined;
    (supervisor.streamChat as ReturnType<typeof vi.fn>).mockImplementation(
      async (_req: unknown, handlers: SidecarChatStreamHandlers) => {
        handlersRef = handlers;
        return 'stream-9';
      },
    );
    // The sidecar answers the cancel with stream.done {cancelled, traceId}.
    (supervisor.cancelChat as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      handlersRef?.onDone?.({
        streamId: 'stream-9',
        ok: false,
        cancelled: true,
        traceId: 'trace_cancelled_1',
      });
    });

    const controller = new AbortController();
    const pending = streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      signal: controller.signal,
      onText: () => {},
    });
    setTimeout(() => controller.abort(), 10);

    const outcome = await pending;
    expect(outcome).toEqual({
      status: 'cancelled',
      reason: 'aborted_by_user',
      traceId: 'trace_cancelled_1',
    });
  });
});

describe('streamCoreLoopTurn / P3.1 orchestration wiring', () => {
  it('forwards the orchestration config onto the wire request', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      orchestration: { maxDepth: 1, maxParallel: 4 },
    });

    expect(sent[0].orchestration).toEqual({ maxDepth: 1, maxParallel: 4 });
  });

  it('forwards explicit compat overrides onto the wire request (W1.3.2)', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      compat: { supportsTemperature: false, maxTokensField: 'max_completion_tokens' },
    });

    expect(sent[0].compat).toEqual({
      supportsTemperature: false,
      maxTokensField: 'max_completion_tokens',
    });
  });

  it('omits the compat field when no overrides are configured', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    expect(sent[0].compat).toBeUndefined();
  });

  it('omits the orchestration field when not configured', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    // Same convention as baseUrl/apiKey: the key may exist with value
    // undefined; JSON-RPC serialization drops it from the wire.
    expect(sent[0].orchestration).toBeUndefined();
  });

  it('routes agent.child lifecycle events to onChildEvent', async () => {
    const { supervisor } = makeSupervisor([
      {
        method: 'child',
        payload: { kind: 'child_spawned', childId: '0.1', task: 'probe', depth: 1 },
      },
      {
        method: 'child',
        payload: { kind: 'child_completed', childId: '0.1', status: 'completed' },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const seen: Array<{ kind: string; childId: string }> = [];

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      orchestration: { maxDepth: 1 },
      onChildEvent: (e) => seen.push({ kind: e.kind, childId: e.childId }),
    });

    expect(seen).toEqual([
      { kind: 'child_spawned', childId: '0.1' },
      { kind: 'child_completed', childId: '0.1' },
    ]);
  });
});

describe('streamCoreLoopTurn / delegate-on-pool subagent wiring', () => {
  it('forwards the subagent config onto the wire request', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      subagent: {
        profiles: { researcher: { toolFilter: ['local_read_file'], concurrent: true } },
      },
    });

    expect(sent[0].subagent).toEqual({
      profiles: { researcher: { toolFilter: ['local_read_file'], concurrent: true } },
    });
  });

  it('omits the subagent field when not configured', async () => {
    const { supervisor, sent } = makeSupervisor([
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);

    await streamCoreLoopTurn({ ...BASE_OPTIONS, supervisor, onText: () => {} });

    // Same convention as orchestration: the key may exist with value
    // undefined; JSON-RPC serialization drops it from the wire.
    expect(sent[0].subagent).toBeUndefined();
  });

  it('routes delegate agent.child events (with profile) to onChildEvent', async () => {
    const { supervisor } = makeSupervisor([
      {
        method: 'child',
        payload: {
          kind: 'child_spawned',
          childId: '0.1',
          task: 'probe',
          depth: 1,
          profile: 'researcher',
        },
      },
      {
        method: 'child',
        payload: { kind: 'child_completed', childId: '0.1', status: 'completed' },
      },
      { method: 'done', payload: { streamId: 'stream-1', ok: true, status: 'completed' } },
    ]);
    const seen: Array<{ kind: string; childId: string; profile?: string }> = [];

    await streamCoreLoopTurn({
      ...BASE_OPTIONS,
      supervisor,
      onText: () => {},
      subagent: true,
      onChildEvent: (e) => seen.push({ kind: e.kind, childId: e.childId, profile: e.profile }),
    });

    expect(seen).toEqual([
      { kind: 'child_spawned', childId: '0.1', profile: 'researcher' },
      { kind: 'child_completed', childId: '0.1', profile: undefined },
    ]);
  });
});
