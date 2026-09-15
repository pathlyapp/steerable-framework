import { describe, expect, it } from 'vitest';
import { detectInterruptedTurn } from '../../src/local-backend/interrupted-helper.js';

// W7-1: 中断检测的三态契约——interrupted（崩溃/强杀：标记残留、无完成记录）
// 提供「继续」入口；cancelled / failed 是活进程写下的终态（标记已清、
// assistant 消息带 completionStatus），一律不误报。
describe('detectInterruptedTurn (W7-1)', () => {
  it('reports a crashed turn: marker survived, no live stream, last message is the unanswered user message', () => {
    expect(
      detectInterruptedTurn({
        turnActive: true,
        streamActive: false,
        lastMessageRole: 'user',
      }),
    ).toBe(true);
  });

  it('does not report a user-cancelled turn: the live process cleared the marker when it persisted completionStatus=cancelled', () => {
    expect(
      detectInterruptedTurn({
        turnActive: false,
        streamActive: false,
        lastMessageRole: 'assistant',
      }),
    ).toBe(false);
  });

  it('does not report a failed turn: the error path also persists an assistant message and clears the marker', () => {
    expect(
      detectInterruptedTurn({
        turnActive: false,
        streamActive: false,
        lastMessageRole: 'assistant',
      }),
    ).toBe(false);
  });

  it('does not report while a stream for the chat is live in this process (re-opened mid-stream)', () => {
    expect(
      detectInterruptedTurn({
        turnActive: true,
        streamActive: true,
        lastMessageRole: 'user',
      }),
    ).toBe(false);
  });

  it('dismisses the stale marker when the reply did persist (crash between the assistant write and the marker clear)', () => {
    expect(
      detectInterruptedTurn({
        turnActive: true,
        streamActive: false,
        lastMessageRole: 'assistant',
      }),
    ).toBe(false);
  });

  it('reports an empty store with a surviving marker (crash before the first flush)', () => {
    expect(
      detectInterruptedTurn({
        turnActive: true,
        streamActive: false,
        lastMessageRole: null,
      }),
    ).toBe(true);
  });
});
