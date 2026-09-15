import { describe, expect, it } from 'vitest';
import {
  choiceFromPresetMode,
  descriptorLabel,
  overrideFromDescriptor,
  overridesEqual,
  presetModeFromChoice,
  summarizePreset,
} from './preset-choice-model';
import type { ProviderPresetDescriptor } from '@/lib/local-api';

// 与框架 llm/presets.py `describe_provider_presets` 同形的注册表行夹具。
const ROWS: ProviderPresetDescriptor[] = [
  {
    host: 'api.deepseek.com',
    modelPrefix: 'deepseek',
    temperature: 0.0,
    topP: null,
    maxTokens: null,
    reasoningEffort: null,
    extraBody: null,
  },
  {
    host: null,
    modelPrefix: 'qwen3',
    temperature: 0.6,
    topP: 0.95,
    maxTokens: null,
    reasoningEffort: null,
    extraBody: { top_k: 20 },
  },
];

describe('overrideFromDescriptor', () => {
  it('drops null fields so they are not sent on the wire', () => {
    expect(overrideFromDescriptor(ROWS[0])).toEqual({ temperature: 0.0 });
    expect(overrideFromDescriptor(ROWS[1])).toEqual({
      temperature: 0.6,
      topP: 0.95,
      extraBody: { top_k: 20 },
    });
  });
});

describe('descriptorLabel', () => {
  it('joins host and model prefix, tolerating single-key rows', () => {
    expect(descriptorLabel(ROWS[0])).toBe('api.deepseek.com + deepseek*');
    expect(descriptorLabel(ROWS[1])).toBe('qwen3*');
  });
});

describe('summarizePreset', () => {
  it('renders a one-line parameter summary', () => {
    expect(summarizePreset(overrideFromDescriptor(ROWS[1]))).toBe(
      'temperature 0.6 · top_p 0.95 · top_k 20',
    );
  });

  it('describes the empty preset honestly (Moonshot case)', () => {
    expect(summarizePreset(null)).toBe('不下发额外采样参数');
    expect(summarizePreset({})).toContain('不带采样字段');
  });
});

describe('presetModeFromChoice / choiceFromPresetMode', () => {
  it('round-trips the three modes', () => {
    expect(presetModeFromChoice(undefined)).toBe('auto');
    expect(presetModeFromChoice({ enabled: true })).toBe('auto');
    expect(presetModeFromChoice({ enabled: false })).toBe('off');
    expect(presetModeFromChoice({ override: { temperature: 0.6 } })).toBe('pinned');

    expect(choiceFromPresetMode('auto', -1, ROWS)).toBeUndefined();
    expect(choiceFromPresetMode('off', -1, ROWS)).toEqual({ enabled: false });
    expect(choiceFromPresetMode('pinned', 1, ROWS)).toEqual({
      override: { temperature: 0.6, topP: 0.95, extraBody: { top_k: 20 } },
    });
  });

  it('keeps a saved custom override when the registry is unavailable', () => {
    const existing = { override: { temperature: 0.42 } };
    expect(choiceFromPresetMode('pinned', -1, [], existing)).toEqual(existing);
    // 无注册表也无已保存覆盖：钉死无从谈起，回落为不持久化（自动）。
    expect(choiceFromPresetMode('pinned', -1, [])).toBeUndefined();
  });
});

describe('overridesEqual', () => {
  it('matches a saved override back to its registry row', () => {
    expect(overridesEqual(overrideFromDescriptor(ROWS[1]), {
      temperature: 0.6,
      topP: 0.95,
      extraBody: { top_k: 20 },
    })).toBe(true);
    expect(overridesEqual(overrideFromDescriptor(ROWS[0]), { temperature: 0.1 })).toBe(false);
  });
});
