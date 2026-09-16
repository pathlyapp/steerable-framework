import { describe, expect, it } from 'vitest';
import {
  getLiveStream,
  liveStreamCount,
  registerLiveStream,
  removeLiveStream,
} from '../../src/local-backend/live-stream';
import type { PersistedTurnBlock } from '../../src/local-backend/turn-timeline';

// 运行中回合的实时快照注册表：切走再切回时 renderer 靠 GET /live-stream
// 恢复「正在运行 + 部分产出」。这里验证注册表的生命周期与「活引用」语义——
// executedActions / timeline / children 是 router 原地 mutate 的同一数组，
// 注册表存引用即可读到最新状态，content 由 onText 显式更新。

describe('live-stream registry', () => {
  it('register → get → remove 生命周期', () => {
    expect(liveStreamCount()).toBe(0);
    const executedActions: Array<Record<string, unknown>> = [];
    const timeline: PersistedTurnBlock[] = [];
    const children: Array<Record<string, unknown>> = [];
    const live = registerLiveStream('chat-1', { executedActions, timeline, children });

    expect(live.status).toBe('running');
    expect(live.content).toBe('');
    expect(getLiveStream('chat-1')).toBe(live);
    expect(liveStreamCount()).toBe(1);

    removeLiveStream('chat-1');
    expect(getLiveStream('chat-1')).toBeUndefined();
    expect(liveStreamCount()).toBe(0);
  });

  it('content 由调用方显式更新', () => {
    const live = registerLiveStream('chat-2', {
      executedActions: [],
      timeline: [],
      children: [],
    });
    live.content = '你好';
    live.content += '，世界';
    expect(getLiveStream('chat-2')?.content).toBe('你好，世界');
    removeLiveStream('chat-2');
  });

  it('executedActions / timeline / children 传引用，原地 mutate 可见', () => {
    const executedActions: Array<Record<string, unknown>> = [];
    const timeline: PersistedTurnBlock[] = [];
    const children: Array<Record<string, unknown>> = [];
    registerLiveStream('chat-3', { executedActions, timeline, children });

    executedActions.push({ id: 'a', tool: 't' });
    timeline.push({ type: 'text', content: 'partial' });
    children.push({ kind: 'child_spawned', childId: '0.1' });

    const snap = getLiveStream('chat-3');
    expect(snap?.executedActions).toHaveLength(1);
    expect(snap?.timeline).toEqual([{ type: 'text', content: 'partial' }]);
    expect(snap?.children).toHaveLength(1);
    removeLiveStream('chat-3');
  });

  it('未注册的 chat 返回 undefined', () => {
    expect(getLiveStream('missing')).toBeUndefined();
  });
});
