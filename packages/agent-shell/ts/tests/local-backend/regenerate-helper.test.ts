import { describe, expect, it } from 'vitest';
import {
  planRegenerateTruncate,
  resolveRegenerateContext,
  resolveRegenerateForkOrdinal,
  type RegenerateMessageLike,
} from '../../src/local-backend/regenerate-helper';

function msg(id: string, role: RegenerateMessageLike['role'], content: string): RegenerateMessageLike {
  return { id, role, content };
}

describe('resolveRegenerateContext', () => {
  it('returns the immediately preceding user message text for a simple exchange', () => {
    const history = [
      msg('u1', 'user', '帮我查一下今天的天气'),
      msg('a1', 'assistant', '今天晴天'),
    ];
    const result = resolveRegenerateContext(history, 'a1');
    expect(result).toEqual({ ok: true, userMessageText: '帮我查一下今天的天气' });
  });

  it('walks back past tool messages to find the real triggering user turn', () => {
    const history = [
      msg('u1', 'user', '运行一下卡片 X'),
      msg('a0', 'assistant', ''),
      msg('t1', 'tool', '{"result":"ok"}'),
      msg('t2', 'tool', '{"result":"ok"}'),
      msg('a1', 'assistant', '已执行完成'),
    ];
    const result = resolveRegenerateContext(history, 'a1');
    expect(result).toEqual({ ok: true, userMessageText: '运行一下卡片 X' });
  });

  it('finds the *nearest* preceding user turn in a multi-round conversation, not the first one', () => {
    const history = [
      msg('u1', 'user', '第一个问题'),
      msg('a1', 'assistant', '第一个回答'),
      msg('u2', 'user', '第二个问题'),
      msg('a2', 'assistant', '第二个回答（要重新生成的）'),
    ];
    const result = resolveRegenerateContext(history, 'a2');
    expect(result).toEqual({ ok: true, userMessageText: '第二个问题' });
  });

  it('falls back to a generic placeholder when no preceding user message exists', () => {
    const history = [msg('a1', 'assistant', '系统自动打的招呼')];
    const result = resolveRegenerateContext(history, 'a1');
    expect(result.ok).toBe(true);
    expect(result.ok && result.userMessageText).toBe('请基于上一轮内容重新生成回复。');
  });

  it('falls back to the placeholder when the preceding user message is empty/whitespace', () => {
    const history = [msg('u1', 'user', '   '), msg('a1', 'assistant', '回复')];
    const result = resolveRegenerateContext(history, 'a1');
    expect(result.ok && result.userMessageText).toBe('请基于上一轮内容重新生成回复。');
  });

  it('rejects when the target message id does not exist', () => {
    const history = [msg('u1', 'user', 'hi'), msg('a1', 'assistant', 'hello')];
    const result = resolveRegenerateContext(history, 'does-not-exist');
    expect(result).toEqual({ ok: false, reason: 'not_found' });
  });

  it('rejects when the target message is not an assistant message', () => {
    const history = [msg('u1', 'user', 'hi'), msg('a1', 'assistant', 'hello')];
    const result = resolveRegenerateContext(history, 'u1');
    expect(result).toEqual({ ok: false, reason: 'not_assistant' });
  });
});

describe('resolveRegenerateForkOrdinal (W5-2 non-destructive regen)', () => {
  it('addresses the prompting user turn of the last assistant reply', () => {
    const history = [
      msg('u1', 'user', '第一个问题'),
      msg('a1', 'assistant', '第一个回答'),
      msg('u2', 'user', '第二个问题'),
      msg('a2', 'assistant', '第二个回答'),
    ];
    expect(resolveRegenerateForkOrdinal(history, 'a2')).toBe(1);
    expect(resolveRegenerateForkOrdinal(history, 'a1')).toBe(0);
  });

  it('does not count tool messages as user turns', () => {
    const history = [
      msg('u1', 'user', '运行卡片'),
      msg('a0', 'assistant', ''),
      msg('t1', 'tool', '{}'),
      msg('a1', 'assistant', '完成'),
    ];
    expect(resolveRegenerateForkOrdinal(history, 'a1')).toBe(0);
  });

  it('returns -1 when no user message precedes the target (fallback case)', () => {
    const history = [msg('a1', 'assistant', '系统招呼')];
    expect(resolveRegenerateForkOrdinal(history, 'a1')).toBe(-1);
  });

  it('returns -1 for an unknown target id', () => {
    const history = [msg('u1', 'user', 'hi'), msg('a1', 'assistant', 'hello')];
    expect(resolveRegenerateForkOrdinal(history, 'nope')).toBe(-1);
  });
});

describe('planRegenerateTruncate', () => {
  it('proceeds when the reply was preserved as a branch', () => {
    expect(planRegenerateTruncate({ ok: true })).toEqual({ proceed: true });
  });

  it('proceeds when the sidecar declined the fork address', () => {
    // Repeated regenerate lands on a branch record whose seed contains the
    // ordinal, and a seed is indivisible — the protocol names host fallback as
    // the expected path here.
    expect(
      planRegenerateTruncate({
        ok: false,
        declined: true,
        reason:
          'invalid_request (-32004): user message index 0 not addressable in record: chat-1:r2',
      }),
    ).toEqual({ proceed: true });
  });

  it('proceeds when no sidecar was attached', () => {
    expect(planRegenerateTruncate(null)).toEqual({ proceed: true });
  });

  it('refuses when the fork failed instead of answering', () => {
    // A timeout, transport failure, or sidecar fault never reached a judgement
    // about the address, so the promise to preserve the reply still stands.
    const plan = planRegenerateTruncate({
      ok: false,
      declined: false,
      reason: 'timeout (-32000): sidecar method agent.session.fork timed out after 10000ms',
    });
    expect(plan.proceed).toBe(false);
    expect(plan).toMatchObject({
      message: expect.stringContaining('agent.session.fork timed out after 10000ms'),
    });
  });

  it('still explains itself when the failure carried no reason', () => {
    const plan = planRegenerateTruncate({ ok: false, declined: false });
    expect(plan).toEqual({
      proceed: false,
      message: '无法重新生成：旧回复未能保留为分支，已放弃改动以免丢失它（未知原因）。请稍后重试。',
    });
  });
});
