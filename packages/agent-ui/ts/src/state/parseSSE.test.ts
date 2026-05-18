/**
 * Tests for `SSEParser` and `parseSSEData`.
 *
 * The parser owns the wire-level framing: split on `\r?\n\r?\n`, accumulate
 * multi-line `data:` payloads, surface `[DONE]` via `onComplete`. We cover the
 * shapes deeppath and deeppath-agent legacy parsers handled, so the cutover
 * doesn't lose any wire variants.
 */

import { describe, expect, it, vi } from 'vitest';
import { SSEParser, parseSSEData } from './parseSSE';

describe('SSEParser', () => {
  it('emits one frame per blank-line-separated block', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('data: {"content":"foo"}\n\ndata: {"content":"bar"}\n\n');
    expect(onFrame).toHaveBeenCalledTimes(2);
    expect(onFrame.mock.calls[0][0].data).toBe('{"content":"foo"}');
    expect(onFrame.mock.calls[1][0].data).toBe('{"content":"bar"}');
  });

  it('handles split chunks across frame boundaries', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('data: {"content":"hello,');
    expect(onFrame).not.toHaveBeenCalled();
    p.feed(' world!"}\n\n');
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(onFrame.mock.calls[0][0].data).toBe('{"content":"hello, world!"}');
  });

  it('joins multi-line data: per SSE spec (newline-joined)', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('data: line one\ndata: line two\n\n');
    expect(onFrame.mock.calls[0][0].data).toBe('line one\nline two');
  });

  it('captures event:, id:, retry:', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('event: error\nid: 42\nretry: 5000\ndata: {"message":"boom"}\n\n');
    const frame = onFrame.mock.calls[0][0];
    expect(frame.event).toBe('error');
    expect(frame.id).toBe('42');
    expect(frame.retry).toBe(5000);
    expect(frame.data).toBe('{"message":"boom"}');
  });

  it('triggers onComplete on the canonical [DONE] terminator', () => {
    const onFrame = vi.fn();
    const onComplete = vi.fn();
    const p = new SSEParser({ onFrame, onComplete });
    p.feed('data: {"content":"x"}\n\ndata: [DONE]\n\n');
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it('triggers onComplete on quoted "[DONE]"', () => {
    const onComplete = vi.fn();
    const p = new SSEParser({ onFrame: () => {}, onComplete });
    p.feed('data: "[DONE]"\n\n');
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it('ignores comment lines (leading colon)', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed(': keep-alive comment\ndata: real\n\n');
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(onFrame.mock.calls[0][0].data).toBe('real');
  });

  it('flushes trailing buffered frame on end()', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('data: orphan'); // no terminator
    expect(onFrame).not.toHaveBeenCalled();
    p.end();
    expect(onFrame).toHaveBeenCalledTimes(1);
    expect(onFrame.mock.calls[0][0].data).toBe('orphan');
  });

  it('supports \\r\\n\\r\\n frame separators (Windows / some proxies)', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.feed('data: one\r\n\r\ndata: two\r\n\r\n');
    expect(onFrame).toHaveBeenCalledTimes(2);
    expect(onFrame.mock.calls[0][0].data).toBe('one');
    expect(onFrame.mock.calls[1][0].data).toBe('two');
  });

  it('stops emitting after cleanup()', () => {
    const onFrame = vi.fn();
    const p = new SSEParser({ onFrame });
    p.cleanup();
    p.feed('data: ignored\n\n');
    expect(onFrame).not.toHaveBeenCalled();
  });

  it('reports parsing exceptions via onError', () => {
    const onError = vi.fn();
    const p = new SSEParser({
      onFrame: () => {
        throw new Error('downstream blew up');
      },
      onError,
    });
    p.feed('data: x\n\n');
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].message).toBe('downstream blew up');
  });
});

describe('parseSSEData', () => {
  it('parses JSON payloads', () => {
    expect(parseSSEData('{"a":1}')).toEqual({ a: 1 });
  });
  it('returns the original string when not JSON (bare text deltas)', () => {
    expect(parseSSEData('hello')).toBe('hello');
  });
});
