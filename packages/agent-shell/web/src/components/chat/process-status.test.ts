import { describe, expect, it } from 'vitest';
import type { TurnBlock } from './turn-timeline';
import {
  activeToolNames,
  estimateTextTokens,
  formatTokenSpeed,
  lastToolsAreRunning,
  processStatusLabel,
} from './process-status';

const reasoning = (content: string): TurnBlock => ({ type: 'reasoning', content });
const tool = (name: string, running = true): TurnBlock => ({
  type: 'tools',
  actions: [{ tool: name, arguments: {}, ...(running ? {} : { result: { success: true } }) }],
});

describe('estimateTextTokens / formatTokenSpeed', () => {
  it('counts CJK cheaper than ASCII and formats tok/s', () => {
    expect(estimateTextTokens('你好世界')).toBe(3);
    expect(estimateTextTokens('abcd')).toBe(1);
    expect(formatTokenSpeed(0, 1000)).toBeNull();
    expect(formatTokenSpeed(40, 200)).toBeNull();
    expect(formatTokenSpeed(40, 1000)).toBe('40 tok/s');
    expect(formatTokenSpeed(3, 1000)).toBe('3 tok/s');
  });
});

describe('active tools', () => {
  it('prefers running tools in the latest tools row', () => {
    const process: TurnBlock[] = [
      tool('csv_get_config', false),
      tool('web_fetch', true),
    ];
    expect(lastToolsAreRunning(process)).toBe(true);
    expect(activeToolNames(process)).toEqual(['web_fetch']);
  });

  it('is not running after the latest tools row has results', () => {
    expect(lastToolsAreRunning([tool('web_fetch', false)])).toBe(false);
  });
});

describe('processStatusLabel', () => {
  it('shows 思考中 plus speed and elapsed while reasoning streams', () => {
    expect(
      processStatusLabel({
        process: [reasoning('先读配置先读配置先读配置先读配置')],
        isStreaming: true,
        elapsedMs: 12_000,
        reasoningElapsedMs: 1000,
      }),
    ).toBe('思考中 · 10 tok/s · 12s');
  });

  it('shows 调用工具中 with the running tool name', () => {
    expect(
      processStatusLabel({
        process: [reasoning('想'), tool('local_run_snippet')],
        isStreaming: true,
        elapsedMs: 5000,
      }),
    ).toBe('调用工具中 · local_run_snippet · 5s');
  });

  it('returns to 思考中 after tools finish and before the next burst', () => {
    expect(
      processStatusLabel({
        process: [tool('local_run_snippet', false)],
        isStreaming: true,
        elapsedMs: 8000,
      }),
    ).toBe('思考中 · 8s');
  });

  it('keeps the finished summary', () => {
    expect(
      processStatusLabel({
        process: [reasoning('先读配置'), tool('web_fetch', false)],
        isStreaming: false,
        elapsedMs: 83_000,
      }),
    ).toBe('1 次工具调用 · 已思考 · 工作了 1m 23s');
  });
});
