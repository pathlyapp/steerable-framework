import { describe, expect, it } from 'vitest';
import type { ExecutedAction } from './ExecutedActionsCard';
import {
  appendDelta,
  countProcessTools,
  fallbackTimeline,
  parseTurnBlocks,
  processHasReasoning,
  sealLastBlock,
  splitTurnProcess,
  syncTools,
} from './turn-timeline';

const tool = (name: string, result?: unknown): ExecutedAction => ({
  tool: name,
  arguments: { path: `/${name}` },
  ...(result === undefined ? {} : { result }),
});

describe('turn-timeline', () => {
  it('merges consecutive deltas of the same kind and splits on kind change', () => {
    let blocks = appendDelta([], 'reasoning', '想');
    blocks = appendDelta(blocks, 'reasoning', '一下');
    blocks = appendDelta(blocks, 'text', '先读文件');
    blocks = appendDelta(blocks, 'text', '。');
    expect(blocks).toEqual([
      { type: 'reasoning', content: '想一下' },
      { type: 'text', content: '先读文件。' },
    ]);
  });

  it('places tools after the text that preceded them, then more text', () => {
    let blocks = appendDelta([], 'text', '开始');
    blocks = syncTools(blocks, [tool('read')]);
    blocks = appendDelta(blocks, 'text', '接着写');
    blocks = syncTools(blocks, [tool('read'), tool('write')]);
    expect(blocks.map((b) => b.type)).toEqual(['text', 'tools', 'text', 'tools']);
    expect(blocks[1]).toEqual({ type: 'tools', actions: [tool('read')] });
    expect(blocks[3]).toEqual({ type: 'tools', actions: [tool('write')] });
  });

  it('groups consecutive tools and updates in-place results', () => {
    const running = tool('read');
    const done = tool('read', { success: true });
    let blocks = syncTools([], [running]);
    blocks = syncTools(blocks, [done]);
    blocks = syncTools(blocks, [done, tool('write')]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toEqual({ type: 'tools', actions: [done, tool('write')] });
  });

  it('ignores empty deltas', () => {
    expect(appendDelta([], 'text', '')).toEqual([]);
  });

  it('starts a new reasoning block after sealLastBlock so rounds stay separate', () => {
    let blocks = appendDelta([], 'reasoning', '第一轮');
    blocks = sealLastBlock(blocks);
    blocks = appendDelta(blocks, 'reasoning', '第二轮');
    expect(blocks).toEqual([
      { type: 'reasoning', content: '第一轮', sealed: true },
      { type: 'reasoning', content: '第二轮' },
    ]);
  });

  it('is a no-op when the last block is already tools or sealed', () => {
    const tools = sealLastBlock([{ type: 'tools', actions: [tool('read')] }]);
    expect(tools).toEqual([{ type: 'tools', actions: [tool('read')] }]);
    const once = sealLastBlock([{ type: 'reasoning', content: '想', sealed: true }]);
    expect(once).toEqual([{ type: 'reasoning', content: '想', sealed: true }]);
  });

  it('falls back to tools-then-text for history without a timeline', () => {
    expect(fallbackTimeline('答', [tool('read', { success: true })])).toEqual([
      { type: 'tools', actions: [tool('read', { success: true })] },
      { type: 'text', content: '答' },
    ]);
    expect(fallbackTimeline('', undefined)).toEqual([]);
  });

  it('splits trailing summary text from the think→act process', () => {
    const blocks = [
      { type: 'reasoning' as const, content: '想' },
      { type: 'tools' as const, actions: [tool('read')] },
      { type: 'text' as const, content: '中间说明' },
      { type: 'tools' as const, actions: [tool('write')] },
      { type: 'text' as const, content: '总结' },
    ];
    expect(splitTurnProcess(blocks)).toEqual({
      process: blocks.slice(0, 4),
      answer: [blocks[4]],
    });
    expect(splitTurnProcess([{ type: 'text', content: '只回答' }])).toEqual({
      process: [],
      answer: [{ type: 'text', content: '只回答' }],
    });
    expect(splitTurnProcess([{ type: 'tools', actions: [tool('read')] }])).toEqual({
      process: [{ type: 'tools', actions: [tool('read')] }],
      answer: [],
    });
    expect(countProcessTools(blocks.slice(0, 4))).toBe(2);
    expect(processHasReasoning(blocks.slice(0, 4))).toBe(true);
  });

  it('keeps every text block in the process until the turn is finalized', () => {
    const blocks = [
      { type: 'reasoning' as const, content: '想' },
      { type: 'text' as const, content: '我先搜' },
      { type: 'reasoning' as const, content: '再写' },
    ];
    expect(splitTurnProcess(blocks, { finalize: false })).toEqual({
      process: blocks,
      answer: [],
    });
  });

  it('parses persisted timeline JSON and rejects unknown kinds', () => {
    const parsed = parseTurnBlocks([
      { type: 'reasoning', content: 'hmm' },
      { type: 'tools', actions: [tool('read')] },
      { type: 'text', content: 'ok' },
    ]);
    expect(parsed).toHaveLength(3);
    expect(parseTurnBlocks([{ type: 'image' }])).toBeNull();
    expect(parseTurnBlocks([])).toBeNull();
  });
});
