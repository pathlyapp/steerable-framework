/**
 * Tests for `useChatSession` — the composer-meets-stream convenience hook.
 */

import { describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { MockChatStreamTransport } from './MockChatStreamTransport';
import { useChatSession } from './useChatSession';

describe('useChatSession', () => {
  it('routes composer.submit through the stream pipeline', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [[
        { type: 'content', content: 'pong' },
        { type: 'done' },
      ]],
    });
    const { result } = renderHook(() =>
      useChatSession({ transport, initialValue: '' }),
    );

    await act(async () => {
      result.current.composer.setValue('ping');
    });
    await act(async () => {
      await result.current.composer.submit();
    });

    await waitFor(() => {
      expect(result.current.messages.length).toBeGreaterThanOrEqual(2);
    });
    expect(result.current.messages[0].content).toBe('ping');
    expect(result.current.composer.value).toBe('');
  });

  it('forwards build metadata onto the transport', async () => {
    const stream = vi.fn().mockImplementation(async (_input, onEvent) => {
      onEvent({ type: 'done' });
      return () => {};
    });
    const { result } = renderHook(() =>
      useChatSession({
        transport: { stream },
        buildMetadata: (v) => ({ length: v.length }),
      }),
    );

    await act(async () => {
      result.current.composer.setValue('hello');
    });
    await act(async () => {
      await result.current.composer.submit();
    });

    expect(stream).toHaveBeenCalledTimes(1);
    expect(stream.mock.calls[0][0]).toEqual({
      content: 'hello',
      metadata: { length: 5 },
    });
  });
});
