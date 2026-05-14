/**
 * Tests for `useAgentSession` — the hook is a thin transport adapter, but we
 * still want to nail down the autoLoad behaviour and current-session pointer
 * because callers route routing decisions off `current?.sessionId`.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { AgentSession } from '@steerable/agent-protocol';
import { useAgentSession, type AgentSessionTransport } from './useAgentSession';

function makeSession(overrides: Partial<AgentSession>): AgentSession {
  return {
    id: 'row1',
    sessionId: 's1',
    userId: 'u1',
    chatId: 'c1',
    currentStage: 'INIT',
    isActive: true,
    createdAt: new Date('2025-01-01').toISOString(),
    updatedAt: new Date('2025-01-01').toISOString(),
    ...overrides,
  };
}

function makeTransport(
  list: AgentSession[] = [],
): { transport: AgentSessionTransport; spies: Record<string, ReturnType<typeof vi.fn>> } {
  const spies = {
    create: vi.fn(async (input: Parameters<AgentSessionTransport['create']>[0]) =>
      makeSession({
        sessionId: 'created_' + input.chatId,
        userId: input.userId,
        chatId: input.chatId,
      }),
    ),
    resume: vi.fn(async (sessionId: string) =>
      makeSession({ sessionId, currentStage: 'RESUMED' }),
    ),
    list: vi.fn(async () => list),
  };
  return { transport: spies as unknown as AgentSessionTransport, spies };
}

describe('useAgentSession', () => {
  it('does not call list when autoLoad is omitted', async () => {
    const { transport, spies } = makeTransport();
    renderHook(() => useAgentSession({ transport }));
    await waitFor(() => {
      expect(spies.list).not.toHaveBeenCalled();
    });
  });

  it('calls list on mount when autoLoad is provided and exposes the result', async () => {
    const sessions = [makeSession({ sessionId: 's1' }), makeSession({ sessionId: 's2' })];
    const { transport, spies } = makeTransport(sessions);
    const { result } = renderHook(() =>
      useAgentSession({
        transport,
        autoLoad: { userId: 'u1', activeOnly: true },
      }),
    );

    await waitFor(() => {
      expect(spies.list).toHaveBeenCalledTimes(1);
      expect(result.current.sessions).toHaveLength(2);
    });
  });

  it('create() pushes the new session in front and updates current', async () => {
    const { transport, spies } = makeTransport([]);
    const { result } = renderHook(() =>
      useAgentSession({ transport, autoLoad: { userId: 'u1' } }),
    );

    await waitFor(() => expect(spies.list).toHaveBeenCalled());

    let created: AgentSession | undefined;
    await act(async () => {
      created = await result.current.create({
        chatId: 'chat-x',
        userId: 'u1',
      });
    });

    expect(created?.sessionId).toBe('created_chat-x');
    await waitFor(() => {
      expect(result.current.current?.sessionId).toBe('created_chat-x');
      expect(result.current.sessions[0]?.sessionId).toBe('created_chat-x');
    });
  });

  it('resume() updates current without mutating sessions', async () => {
    const list = [makeSession({ sessionId: 's-old' })];
    const { transport, spies } = makeTransport(list);
    const { result } = renderHook(() =>
      useAgentSession({ transport, autoLoad: { userId: 'u1' } }),
    );

    await waitFor(() => expect(spies.list).toHaveBeenCalled());

    await act(async () => {
      await result.current.resume('s-old');
    });

    expect(result.current.current?.sessionId).toBe('s-old');
    expect(result.current.current?.currentStage).toBe('RESUMED');
    expect(result.current.sessions).toHaveLength(1);
  });

  it('captures errors from list() into the error field without throwing', async () => {
    const transport: AgentSessionTransport = {
      create: vi.fn(),
      resume: vi.fn(),
      list: vi.fn(async () => {
        throw new Error('db down');
      }),
    };
    const { result } = renderHook(() =>
      useAgentSession({ transport, autoLoad: { userId: 'u1' } }),
    );

    await waitFor(() => {
      expect(result.current.error?.message).toBe('db down');
    });
  });
});
