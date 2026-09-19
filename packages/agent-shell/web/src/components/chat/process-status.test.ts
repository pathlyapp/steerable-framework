import { describe, expect, it } from 'vitest';
import type { TurnBlock } from './turn-timeline';
import {
  activeToolNames,
  estimateTextTokens,
  estimateTurnTokens,
  formatTokenSpeed,
  lastToolsAreRunning,
  llmRequestElapsedMs,
  LlmRequestSpeedTracker,
  parseLlmSpeedPayload,
  processStatusLabel,
  thinkingFoldLabel,
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
  it('is only 工作中 while the turn is streaming', () => {
    expect(
      processStatusLabel({
        process: [reasoning('先读配置先读配置先读配置先读配置')],
        isStreaming: true,
        elapsedMs: 12_000,
      }),
    ).toBe('工作中');
    expect(
      processStatusLabel({
        process: [reasoning('想'), tool('local_run_snippet')],
        isStreaming: true,
        elapsedMs: 5000,
      }),
    ).toBe('工作中');
    expect(
      processStatusLabel({
        process: [reasoning('想'), { type: 'text', content: '我先搜工具' }],
        isStreaming: true,
        elapsedMs: 4000,
      }),
    ).toBe('工作中');
  });

  it('summarizes thinking rounds, tool calls, and duration after the turn ends', () => {
    expect(
      processStatusLabel({
        process: [reasoning('先读配置'), tool('web_fetch', false), reasoning('再写')],
        isStreaming: false,
        elapsedMs: 83_000,
      }),
    ).toBe('思考 2 次 · 工具调用 1 次 · 工作了 1m 23s');
  });

  it('omits empty counts and sub-second work time', () => {
    expect(
      processStatusLabel({
        process: [tool('web_fetch', false)],
        isStreaming: false,
        elapsedMs: 400,
      }),
    ).toBe('工具调用 1 次');
  });
});

describe('thinkingFoldLabel', () => {
  it('shows 思考中 plus elapsed while that round is live', () => {
    expect(
      thinkingFoldLabel({
        content: '先读配置先读配置先读配置先读配置',
        isLive: true,
        elapsedMs: 1000,
      }),
    ).toBe('思考中 · 1s');
  });

  it('estimates turn tokens from reasoning and text', () => {
    expect(
      estimateTurnTokens([
        { type: 'reasoning', content: '先读配置先读配置先读配置先读配置' },
        { type: 'text', content: '问好完成。' },
      ]),
    ).toBe(13);
    expect(estimateTurnTokens([], 'abcd')).toBe(1);
  });

  it('freezes to 思考 · duration after that round ends', () => {
    expect(
      thinkingFoldLabel({
        content: '先读配置',
        isLive: false,
        elapsedMs: 8000,
      }),
    ).toBe('思考 · 8s');
  });

  it('is just 思考 when a finished round has no recorded duration', () => {
    expect(thinkingFoldLabel({ content: '先读配置', isLive: false })).toBe('思考');
  });
});

describe('LlmRequestSpeedTracker', () => {
  it('counts every model stage and excludes the gap between requests', () => {
    const tracker = new LlmRequestSpeedTracker();
    tracker.noteOutput('思考思考思考思考', 1_000);
    tracker.noteOutput('回答回答回答回答', 1_500);
    tracker.endRequest(2_000);
    tracker.noteOutput('结论结论结论结论', 5_000);
    const live = tracker.snapshot(5_200);
    expect(live.live).toBe(true);
    expect(live.tokens).toBe(15);
    expect(live.elapsedMs).toBe(1_200);
    const done = tracker.endRequest(5_500);
    expect(done.live).toBe(false);
    expect(done.elapsedMs).toBe(1_500);
    expect(done.tokens).toBe(15);
    expect(llmRequestElapsedMs(done, 9_000)).toBe(1_500);
    expect(formatTokenSpeed(done.tokens, done.elapsedMs)).toBe('10 tok/s');
  });

  it('parses an llm_speed payload', () => {
    expect(
      parseLlmSpeedPayload({
        tokens: 8,
        elapsedMs: 800,
        live: true,
        closedMs: 200,
        requestStartedAt: 100,
      }),
    ).toEqual({
      tokens: 8,
      elapsedMs: 800,
      live: true,
      closedMs: 200,
      requestStartedAt: 100,
    });
    expect(parseLlmSpeedPayload({ tokens: 1 })).toBeNull();
  });
});
