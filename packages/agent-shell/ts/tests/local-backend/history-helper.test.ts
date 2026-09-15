import { describe, it, expect } from 'vitest';
import { dropCurrentUserMessage } from '../../src/local-backend/history-helper.js';

function msg(id: string, role: 'user' | 'assistant') {
  return { id, role };
}

describe('dropCurrentUserMessage', () => {
  it('send 路径按 id 剔除当前用户消息（常规：它在尾部）', () => {
    const history = [msg('u1', 'user'), msg('a1', 'assistant'), msg('u2', 'user')];
    expect(dropCurrentUserMessage(history, 'u2')).toEqual([msg('u1', 'user'), msg('a1', 'assistant')]);
  });

  it('send 路径按 id 剔除——上一条 assistant 回复迟落库排到尾部时也不漏判', () => {
    // 2026-08-28 E2E 录制实锤的竞态：a1 的 createdAt 晚于 u2，尾序变成
    // [u1, u2, a1]，按位置 pop 会漏掉 u2，导致 u2 被重复注入。
    const history = [msg('u1', 'user'), msg('u2', 'user'), msg('a1', 'assistant')];
    expect(dropCurrentUserMessage(history, 'u2')).toEqual([msg('u1', 'user'), msg('a1', 'assistant')]);
  });

  it('send 路径：历史里没有该 id 时原样返回', () => {
    const history = [msg('u1', 'user'), msg('a1', 'assistant')];
    expect(dropCurrentUserMessage(history, 'u-missing')).toEqual(history);
  });

  it('regenerate 路径（无 id）：剔除尾部的触发用户消息', () => {
    const history = [msg('u1', 'user'), msg('a1', 'assistant'), msg('u2', 'user')];
    expect(dropCurrentUserMessage(history, undefined)).toEqual([
      msg('u1', 'user'),
      msg('a1', 'assistant'),
    ]);
  });

  it('regenerate 路径（无 id）：尾部是 assistant 时不动历史', () => {
    const history = [msg('u1', 'user'), msg('a1', 'assistant')];
    expect(dropCurrentUserMessage(history, undefined)).toEqual(history);
  });

  it('空历史安全返回', () => {
    expect(dropCurrentUserMessage([], 'u1')).toEqual([]);
    expect(dropCurrentUserMessage([], undefined)).toEqual([]);
  });
});
