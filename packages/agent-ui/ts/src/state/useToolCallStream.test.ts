/**
 * Tests for `useToolCallStream` — pure derivation of in-flight tool-call
 * status from a `ChatMessage`.
 */

import { describe, expect, it } from 'vitest';
import { renderHook } from '@testing-library/react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { useToolCallStream } from './useToolCallStream';

const baseMsg: ChatMessage = {
  id: 'm1',
  role: 'assistant',
  content: '',
  createdAt: new Date().toISOString(),
};

describe('useToolCallStream', () => {
  it('returns an empty entry list when the message has no tool calls', () => {
    const { result } = renderHook(() => useToolCallStream({ message: baseMsg }));
    expect(result.current.entries).toEqual([]);
    expect(result.current.pendingCount).toBe(0);
  });

  it('classifies pending calls (no result yet) as pending', () => {
    const msg: ChatMessage = {
      ...baseMsg,
      toolCalls: [{ id: 'c1', name: 'get_weather', arguments: {} }],
    };
    const { result } = renderHook(() => useToolCallStream({ message: msg }));
    expect(result.current.entries[0].status).toBe('pending');
    expect(result.current.pendingCount).toBe(1);
  });

  it('infers mode from the tool name', () => {
    const msg: ChatMessage = {
      ...baseMsg,
      toolCalls: [
        { id: 'r', name: 'get_user', arguments: {} },
        { id: 'd', name: 'delete_user', arguments: {} },
        { id: 'l', name: 'local_exec', arguments: {} },
        { id: 'w', name: 'create_thing', arguments: {} },
      ],
    };
    const { result } = renderHook(() => useToolCallStream({ message: msg }));
    expect(result.current.entries.map((e) => e.mode)).toEqual([
      'read',
      'destructive',
      'local',
      'safe_write',
    ]);
    expect(result.current.entries[1].isDestructive).toBe(true);
    expect(result.current.entries[2].requiresApproval).toBe(true);
  });

  it('overrides mode via modeByName', () => {
    const msg: ChatMessage = {
      ...baseMsg,
      toolCalls: [{ id: 'c1', name: 'mystery', arguments: {} }],
    };
    const { result } = renderHook(() =>
      useToolCallStream({ message: msg, modeByName: { mystery: 'destructive' } }),
    );
    expect(result.current.entries[0].mode).toBe('destructive');
  });

  it('pairs results via resultByCallId, falls back to message.toolResult', () => {
    const msg: ChatMessage = {
      ...baseMsg,
      toolCalls: [
        { id: 'a', name: 'get_a', arguments: {} },
        { id: 'b', name: 'get_b', arguments: {} },
      ],
      toolResult: { success: true, data: { fallback: true } },
    };
    const { result } = renderHook(() =>
      useToolCallStream({
        message: msg,
        resultByCallId: { a: { success: false, data: { err: 'x' } } },
      }),
    );
    expect(result.current.entries[0].status).toBe('error');
    expect(result.current.entries[1].status).toBe('done');
    expect(result.current.errorCount).toBe(1);
  });
});
