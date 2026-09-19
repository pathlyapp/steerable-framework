import { describe, expect, it } from 'vitest';
import {
  appendTimelineDelta,
  sealLastTimelineBlock,
  syncTimelineTools,
  type PersistedTurnBlock,
} from '../../src/local-backend/turn-timeline.js';

describe('local-backend turn-timeline', () => {
  it('interleaves text and tools in call order', () => {
    const blocks: PersistedTurnBlock[] = [];
    appendTimelineDelta(blocks, 'reasoning', '想');
    appendTimelineDelta(blocks, 'text', '读文件');
    syncTimelineTools(blocks, [{ tool: 'read' }]);
    appendTimelineDelta(blocks, 'text', '写回去');
    syncTimelineTools(blocks, [{ tool: 'read' }, { tool: 'write' }]);
    expect(blocks.map((b) => b.type)).toEqual(['reasoning', 'text', 'tools', 'text', 'tools']);
    expect(blocks[2]).toEqual({ type: 'tools', actions: [{ tool: 'read' }] });
    expect(blocks[4]).toEqual({ type: 'tools', actions: [{ tool: 'write' }] });
  });

  it('starts a new reasoning block after sealLastTimelineBlock', () => {
    const blocks: PersistedTurnBlock[] = [];
    appendTimelineDelta(blocks, 'reasoning', '第一轮');
    sealLastTimelineBlock(blocks);
    appendTimelineDelta(blocks, 'reasoning', '第二轮');
    expect(blocks).toEqual([
      { type: 'reasoning', content: '第一轮', sealed: true },
      { type: 'reasoning', content: '第二轮' },
    ]);
  });
});
