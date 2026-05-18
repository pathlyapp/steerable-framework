/**
 * Tests for `MockChatStreamTransport`.
 *
 * The mock is the source of all canned demos / stories / fixtures, so a
 * regression here cascades. We cover: per-turn script rotation, exhaustion
 * policies, the cancel handle, and per-step delays.
 */

import { describe, expect, it, vi } from 'vitest';
import { MockChatStreamTransport } from './MockChatStreamTransport';

describe('MockChatStreamTransport', () => {
  it('replays the script for the current turn', async () => {
    const onEvent = vi.fn();
    const transport = new MockChatStreamTransport({
      scripts: [[
        { type: 'content', content: 'a' },
        { type: 'content', content: 'b' },
        { type: 'done' },
      ]],
    });
    await transport.stream({ content: 'hi' }, onEvent);
    // The mock dispatches synchronously when defaultDelayMs is 0 — but it
    // still goes through a microtask queue inside `run()`. Flush.
    await new Promise((r) => setTimeout(r, 0));
    expect(onEvent).toHaveBeenCalledTimes(3);
    expect(onEvent.mock.calls.map((c) => c[0].type)).toEqual([
      'content',
      'content',
      'done',
    ]);
  });

  it('cycles scripts when more turns than scripts exist (default)', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [
        [{ type: 'content', content: 'a' }, { type: 'done' }],
        [{ type: 'content', content: 'b' }, { type: 'done' }],
      ],
    });
    const sink: string[] = [];
    const onEvent = (e: any) => {
      if (e.type === 'content') sink.push(e.content);
    };
    await transport.stream({ content: '' }, onEvent);
    await transport.stream({ content: '' }, onEvent);
    await transport.stream({ content: '' }, onEvent);
    await new Promise((r) => setTimeout(r, 0));
    expect(sink).toEqual(['a', 'b', 'a']);
  });

  it('exhaustionPolicy=last keeps emitting the last script', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [
        [{ type: 'content', content: 'first' }, { type: 'done' }],
        [{ type: 'content', content: 'last' }, { type: 'done' }],
      ],
      exhaustionPolicy: 'last',
    });
    const sink: string[] = [];
    const onEvent = (e: any) => {
      if (e.type === 'content') sink.push(e.content);
    };
    await transport.stream({ content: '' }, onEvent);
    await transport.stream({ content: '' }, onEvent);
    await transport.stream({ content: '' }, onEvent);
    await new Promise((r) => setTimeout(r, 0));
    expect(sink).toEqual(['first', 'last', 'last']);
  });

  it('exhaustionPolicy=empty emits a bare done event', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [[{ type: 'content', content: 'one' }, { type: 'done' }]],
      exhaustionPolicy: 'empty',
    });
    const events: string[] = [];
    await transport.stream({ content: '' }, (e) => events.push(e.type));
    await transport.stream({ content: '' }, (e) => events.push(e.type));
    await new Promise((r) => setTimeout(r, 0));
    expect(events).toEqual(['content', 'done', 'done']);
  });

  it('accepts a callback in place of a script array', async () => {
    const transport = new MockChatStreamTransport({
      scripts: (turn, input) => [
        { type: 'content', content: `${input.content}:${turn}` },
        { type: 'done' },
      ],
    });
    const sink: string[] = [];
    await transport.stream({ content: 'hi' }, (e) => {
      if (e.type === 'content') sink.push(e.content!);
    });
    await transport.stream({ content: 'yo' }, (e) => {
      if (e.type === 'content') sink.push(e.content!);
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(sink).toEqual(['hi:0', 'yo:1']);
  });

  it('honours per-step delayMs', async () => {
    vi.useFakeTimers();
    try {
      const transport = new MockChatStreamTransport({
        scripts: [[
          { event: { type: 'content', content: 'fast' } },
          { event: { type: 'content', content: 'slow' }, delayMs: 100 },
          { type: 'done' },
        ]],
      });
      const events: any[] = [];
      const p = transport.stream({ content: '' }, (e) => events.push(e));
      await p;
      // After the immediate microtask: first event delivered.
      await Promise.resolve();
      expect(events.length).toBeLessThanOrEqual(2);
      await vi.advanceTimersByTimeAsync(100);
      // After the delay: all events delivered.
      expect(events.length).toBe(3);
      expect(events[1].content).toBe('slow');
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancel handle stops further events', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [[
        { event: { type: 'content', content: 'a' }, delayMs: 10 },
        { event: { type: 'content', content: 'b' }, delayMs: 10 },
        { type: 'done' },
      ]],
    });
    const events: any[] = [];
    const cancel = await transport.stream({ content: '' }, (e) => events.push(e));
    cancel();
    await new Promise((r) => setTimeout(r, 50));
    // Cancel should have prevented at least one of the delayed events from
    // arriving. We don't pin the exact count (timing-dependent), just the
    // strict upper bound.
    expect(events.length).toBeLessThan(3);
  });

  it('reset() rewinds the turn counter', async () => {
    const transport = new MockChatStreamTransport({
      scripts: [
        [{ type: 'content', content: 'a' }, { type: 'done' }],
        [{ type: 'content', content: 'b' }, { type: 'done' }],
      ],
      exhaustionPolicy: 'empty',
    });
    const sink: string[] = [];
    await transport.stream({ content: '' }, (e) => {
      if (e.type === 'content') sink.push(e.content!);
    });
    transport.reset();
    await transport.stream({ content: '' }, (e) => {
      if (e.type === 'content') sink.push(e.content!);
    });
    await new Promise((r) => setTimeout(r, 0));
    expect(sink).toEqual(['a', 'a']);
  });
});
