import { afterEach, describe, expect, it } from 'vitest';
import {
  canonicalizeModelId,
  isForcedToolChoiceRejected,
  modelLikelyRejectsForcedToolChoice,
  rememberForcedToolChoiceRejected,
  resetForcedToolChoiceCompatForTests,
  resolveOpenAiToolChoice,
} from '../../src/llm/tool-choice';

describe('canonicalizeModelId()', () => {
  it('strips gateway prefixes and lowercases', () => {
    expect(canonicalizeModelId('OpenAI/DeepSeek-V4-Flash')).toBe('deepseek-v4-flash');
    expect(canonicalizeModelId('  o3-mini  ')).toBe('o3-mini');
  });
});

describe('modelLikelyRejectsForcedToolChoice()', () => {
  it.each([
    ['vendor-reasoner', true],
    ['foo-reasoning-bar', true],
    ['claude-sonnet-thinking', true],
    ['deepseek-reasoner', true],
    ['o1', true],
    ['o3-mini', true],
    ['o4-mini', true],
    ['gpt-4o', false],
    ['deepseek-chat', false],
    ['deepseek-v4-flash', false],
    [undefined, false],
  ])('%s → %s', (model, expected) => {
    expect(modelLikelyRejectsForcedToolChoice(model)).toBe(expected);
  });
});

describe('resolveOpenAiToolChoice()', () => {
  afterEach(() => {
    resetForcedToolChoiceCompatForTests();
  });

  it('rewrites required → auto for ids that advertise thinking/reasoner', () => {
    expect(resolveOpenAiToolChoice('some-vendor-reasoner', 'required')).toBe('auto');
    expect(resolveOpenAiToolChoice('o3-mini', 'required')).toBe('auto');
  });

  it('leaves auto / none unchanged', () => {
    expect(resolveOpenAiToolChoice('o3-mini', 'auto')).toBe('auto');
    expect(resolveOpenAiToolChoice('o3-mini', 'none')).toBe('none');
    expect(resolveOpenAiToolChoice('o3-mini', undefined)).toBe('auto');
  });

  it('keeps required until a 400 teaches the cache, including unnamed thinking models', () => {
    expect(resolveOpenAiToolChoice('deepseek-chat', 'required')).toBe('required');
    expect(resolveOpenAiToolChoice('deepseek-v4-flash', 'required')).toBe('required');
    rememberForcedToolChoiceRejected('openai/deepseek-v4-flash');
    expect(resolveOpenAiToolChoice('deepseek-v4-flash', 'required')).toBe('auto');
  });
});

describe('isForcedToolChoiceRejected()', () => {
  it('matches thinking / support errors that mention tool_choice', () => {
    expect(
      isForcedToolChoiceRejected(400, 'Thinking mode does not support this tool_choice'),
    ).toBe(true);
    expect(
      isForcedToolChoiceRejected(400, '{"error":{"message":"does not support this tool_choice"}}'),
    ).toBe(true);
    expect(
      isForcedToolChoiceRejected(400, 'tool_choice cannot be used with thinking'),
    ).toBe(true);
  });

  it('ignores unrelated 400s and non-400 statuses', () => {
    expect(
      isForcedToolChoiceRejected(
        400,
        "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'",
      ),
    ).toBe(false);
    expect(isForcedToolChoiceRejected(401, 'Thinking mode does not support this tool_choice')).toBe(
      false,
    );
  });
});
