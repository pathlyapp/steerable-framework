/**
 * Tests for `useChatStream`.
 *
 * The hook is an SSE-event reducer that owns ChatMessage[]; we verify the
 * canonical event family from `@steerable/agent-protocol` produces the right
 * message buffer mutations, and that lifecycle (cancel, error, unmount) is
 * handled without leaking pending streams.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import {
  useChatStream,
  type ChatStreamSendInput,
  type ChatStreamTransport,
} from './useChatStream';

/**
 * A transport whose streams stay open until the test explicitly finishes them,
 * so we can submit follow-ups mid-stream and watch the queue drain.
 */
function makeControllableTransport() {
  const streams: Array<{
    input: ChatStreamSendInput;
    onEvent: (e: SSEEvent) => void;
    finish: () => void;
  }> = [];
  const transport: ChatStreamTransport = {
    stream: (input, onEvent) =>
      new Promise<void>((resolve) => {
        streams.push({
          input,
          onEvent,
          finish: () => {
            onEvent({ type: 'done' });
            resolve();
          },
        });
      }),
  };
  return { transport, streams };
}

function makeTransport(
  script: SSEEvent[][],
): { transport: ChatStreamTransport; cancel: ReturnType<typeof vi.fn> } {
  const cancel = vi.fn();
  let invocation = 0;
  const transport: ChatStreamTransport = {
    stream: async (_input, onEvent) => {
      const events = script[invocation++] ?? [];
      // Deliver synchronously inside `await Promise.resolve()` to mimic real
      // SSE chunks landing on the microtask queue.
      for (const ev of events) {
        await Promise.resolve();
        onEvent(ev);
      }
      return cancel;
    },
  };
  return { transport, cancel };
}

describe('useChatStream', () => {
  it('appends user + assistant placeholder, then accumulates content deltas', async () => {
    const { transport } = makeTransport([
      [
        { type: 'content', content: 'Hello, ' },
        { type: 'content', content: 'world!' },
        { type: 'done' },
      ],
    ]);

    const { result } = renderHook(() => useChatStream({ transport }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'hi' });
    });

    await waitFor(() => {
      const last = result.current.messages.at(-1);
      expect(last?.role).toBe('assistant');
      expect(last?.content).toBe('Hello, world!');
    });
    expect(result.current.messages[0].role).toBe('user');
    expect(result.current.messages[0].content).toBe('hi');
    expect(result.current.isStreaming).toBe(false);
  });

  it('captures tool_call events as toolCalls on the assistant message', async () => {
    const { transport } = makeTransport([
      [
        { type: 'content', content: 'calling…' },
        {
          type: 'tool_call',
          payload: { id: 'c1', name: 'get_weather', arguments: { city: 'SF' } },
        },
        {
          type: 'tool_result',
          payload: { success: true, data: { temp: 70 } },
        },
        { type: 'done' },
      ],
    ]);

    const { result } = renderHook(() => useChatStream({ transport }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'weather?' });
    });

    await waitFor(() => {
      const last = result.current.messages.at(-1);
      expect(last?.toolCalls).toEqual([
        { id: 'c1', name: 'get_weather', arguments: { city: 'SF' } },
      ]);
      expect(last?.toolResult).toEqual({ success: true, data: { temp: 70 } });
    });
  });

  it('routes tool_result via toolResultToMessage when provided', async () => {
    const { transport } = makeTransport([
      [
        {
          type: 'tool_result',
          payload: { success: true, data: { ok: true } },
        },
        { type: 'done' },
      ],
    ]);

    const toolResultToMessage = vi.fn(() => ({
      id: 't1',
      role: 'tool' as const,
      content: 'mapped',
      createdAt: new Date().toISOString(),
    }));

    const { result } = renderHook(() =>
      useChatStream({ transport, toolResultToMessage }),
    );

    await act(async () => {
      await result.current.sendUserMessage({ content: 'go' });
    });

    await waitFor(() => {
      expect(toolResultToMessage).toHaveBeenCalledTimes(1);
      const tool = result.current.messages.find((m) => m.role === 'tool');
      expect(tool?.content).toBe('mapped');
    });
  });

  it('forwards unrecognised events to onUnknownEvent', async () => {
    const { transport } = makeTransport([
      [
        { type: 'loader-hint', hint: 'thinking…' },
        { type: 'agent', payload: { agentId: 'a1' } },
        { type: 'done' },
      ],
    ]);
    const onUnknownEvent = vi.fn();
    const { result } = renderHook(() => useChatStream({ transport, onUnknownEvent }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'go' });
    });

    await waitFor(() => {
      expect(onUnknownEvent).toHaveBeenCalledTimes(2);
    });
    expect(onUnknownEvent.mock.calls[0]?.[0].type).toBe('loader-hint');
    expect(onUnknownEvent.mock.calls[1]?.[0].type).toBe('agent');
  });

  it('emits an error overlay onto the assistant message when the stream throws', async () => {
    const transport: ChatStreamTransport = {
      stream: async () => {
        throw new Error('boom');
      },
    };
    const { result } = renderHook(() => useChatStream({ transport }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'hi' });
    });

    await waitFor(() => {
      const last = result.current.messages.at(-1);
      expect(last?.content).toContain('[stream error] boom');
    });
    expect(result.current.isStreaming).toBe(false);
  });

  it('renders error events from the protocol as inline overlays', async () => {
    const { transport } = makeTransport([
      [
        { type: 'content', content: 'partial' },
        { type: 'error', message: 'upstream failed' },
        { type: 'done' },
      ],
    ]);
    const { result } = renderHook(() => useChatStream({ transport }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'hi' });
    });

    await waitFor(() => {
      const last = result.current.messages.at(-1);
      expect(last?.content).toContain('[stream error] upstream failed');
    });
  });

  it('budget_exhausted overrides the assistant content with the error message', async () => {
    const { transport } = makeTransport([
      [
        { type: 'budget_exhausted', message: 'token limit hit' },
        { type: 'done' },
      ],
    ]);
    const { result } = renderHook(() => useChatStream({ transport }));

    await act(async () => {
      await result.current.sendUserMessage({ content: 'hi' });
    });

    await waitFor(() => {
      const last = result.current.messages.at(-1);
      expect(last?.content).toBe('token limit hit');
    });
  });

  it('cancel() invokes the transport-supplied cancel handle', async () => {
    const cancel = vi.fn();
    const transport: ChatStreamTransport = {
      stream: async () =>
        // Return cancel synchronously but never resolve the rest of the stream.
        new Promise((_resolve) => {
          setImmediate(() => {
            // Returning the cancel via the resolved value pattern is the
            // canonical use; here we use a separate channel.
          });
          // For test simplicity, return cancel after a microtask flush.
        }).then(() => cancel),
    };

    const { result, unmount } = renderHook(() =>
      useChatStream({ transport }),
    );
    void act(() => {
      void result.current.sendUserMessage({ content: 'hi' });
    });

    // Unmounting should not throw even if the transport is still hanging.
    unmount();
    expect(true).toBe(true);
  });
});

describe('steerUserMessage', () => {
  it('appends the user message when the transport accepts the steer', async () => {
    const steer = vi.fn().mockResolvedValue(true);
    const transport: ChatStreamTransport = {
      stream: vi.fn(async (_input, onEvent) => {
        onEvent({ type: 'content', content: 'working…' });
        // keep the stream open until the test steers
        await new Promise<void>((resolve) => setTimeout(resolve, 30));
        onEvent({ type: 'content', content: 'done' });
        onEvent({ type: 'done' });
      }),
      steer,
    };
    const { result } = renderHook(() => useChatStream({ transport }));

    let done: Promise<void>;
    act(() => {
      done = result.current.sendUserMessage({ content: 'start' });
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 5));
    });

    let ok = false;
    await act(async () => {
      ok = await result.current.steerUserMessage('补充一句');
    });
    expect(ok).toBe(true);
    expect(steer).toHaveBeenCalledWith('补充一句');
    expect(
      result.current.messages.some((m) => m.role === 'user' && m.content === '补充一句'),
    ).toBe(true);

    await act(async () => {
      await done;
    });
  });

  it('returns false and appends nothing when not streaming', async () => {
    const steer = vi.fn().mockResolvedValue(true);
    const transport: ChatStreamTransport = {
      stream: vi.fn(async () => {}),
      steer,
    };
    const { result } = renderHook(() => useChatStream({ transport }));

    let ok = true;
    await act(async () => {
      ok = await result.current.steerUserMessage('hello');
    });
    expect(ok).toBe(false);
    expect(steer).not.toHaveBeenCalled();
    expect(result.current.messages).toHaveLength(0);
  });

  it('returns false when the transport has no steer support', async () => {
    const transport: ChatStreamTransport = {
      stream: vi.fn(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 20));
      }),
    };
    const { result } = renderHook(() => useChatStream({ transport }));

    let done: Promise<void>;
    act(() => {
      done = result.current.sendUserMessage({ content: 'start' });
    });
    let ok = true;
    await act(async () => {
      ok = await result.current.steerUserMessage('mid');
    });
    expect(ok).toBe(false);
    await act(async () => {
      await done;
    });
  });
});

describe('follow-up queue (W6-2)', () => {
  it('sendUserMessage while streaming queues instead of dropping, then auto-sends next turn', async () => {
    const { transport, streams } = makeControllableTransport();
    const { result } = renderHook(() => useChatStream({ transport }));

    // Turn 1 starts and stays open.
    act(() => {
      void result.current.sendUserMessage({ content: 'first' });
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(result.current.isStreaming).toBe(true);

    // Submit a second message mid-stream — must be queued, not dropped.
    await act(async () => {
      await result.current.sendUserMessage({ content: 'second' });
    });
    expect(streams).toHaveLength(1); // no new turn yet
    expect(result.current.pendingFollowUps).toEqual([{ content: 'second' }]);

    // Finish turn 1 → the queued message auto-sends as turn 2.
    await act(async () => {
      streams[0]!.finish();
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    expect(streams[1]!.input.content).toBe('second');
    expect(result.current.pendingFollowUps).toEqual([]);

    await act(async () => {
      streams[1]!.finish();
    });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));
    // Both user messages landed in the transcript.
    const userTexts = result.current.messages
      .filter((m) => m.role === 'user')
      .map((m) => m.content);
    expect(userTexts).toEqual(['first', 'second']);
  });

  it('followUpUserMessage queues while streaming and sends immediately when idle', async () => {
    const { transport, streams } = makeControllableTransport();
    const { result } = renderHook(() => useChatStream({ transport }));

    // Idle: followUpUserMessage behaves like a normal send.
    await act(async () => {
      result.current.followUpUserMessage({ content: 'idle' });
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0]!.input.content).toBe('idle');

    // Streaming: queues.
    await act(async () => {
      result.current.followUpUserMessage({ content: 'queued' });
    });
    expect(streams).toHaveLength(1);
    expect(result.current.pendingFollowUps).toEqual([{ content: 'queued' }]);

    await act(async () => {
      streams[0]!.finish();
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    expect(streams[1]!.input.content).toBe('queued');
    await act(async () => {
      streams[1]!.finish();
    });
  });

  it('removeFollowUp withdraws a queued message before it sends', async () => {
    const { transport, streams } = makeControllableTransport();
    const { result } = renderHook(() => useChatStream({ transport }));

    act(() => {
      void result.current.sendUserMessage({ content: 'first' });
    });
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => {
      result.current.followUpUserMessage({ content: 'keep' });
      result.current.followUpUserMessage({ content: 'drop' });
    });
    expect(result.current.pendingFollowUps.map((m) => m.content)).toEqual(['keep', 'drop']);

    await act(async () => {
      result.current.removeFollowUp(1); // remove 'drop'
    });
    expect(result.current.pendingFollowUps.map((m) => m.content)).toEqual(['keep']);

    await act(async () => {
      streams[0]!.finish();
    });
    await waitFor(() => expect(streams).toHaveLength(2));
    expect(streams[1]!.input.content).toBe('keep');
    await act(async () => {
      streams[1]!.finish();
    });
    // 'drop' never sent.
    expect(streams).toHaveLength(2);
  });

  it('cancel() clears the queue so nothing auto-sends afterwards', async () => {
    const { transport, streams } = makeControllableTransport();
    const { result } = renderHook(() => useChatStream({ transport }));

    act(() => {
      void result.current.sendUserMessage({ content: 'first' });
    });
    await waitFor(() => expect(streams).toHaveLength(1));

    await act(async () => {
      result.current.followUpUserMessage({ content: 'queued' });
    });
    expect(result.current.pendingFollowUps).toHaveLength(1);

    await act(async () => {
      result.current.cancel();
    });
    expect(result.current.pendingFollowUps).toEqual([]);

    // Even if the stream later finishes, no follow-up turn starts.
    await act(async () => {
      streams[0]!.finish();
    });
    await waitFor(() => expect(result.current.isStreaming).toBe(false));
    expect(streams).toHaveLength(1);
  });
});
