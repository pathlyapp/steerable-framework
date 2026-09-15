import { describe, expect, it } from 'vitest';
import type { GatewayModelEntry } from '@/lib/local-api';
import {
  formatContextWindow,
  modelCapabilityChips,
} from './model-capabilities';

function entry(overrides: Partial<GatewayModelEntry> & Pick<GatewayModelEntry, 'id'>): GatewayModelEntry {
  return {
    name: null,
    window: null,
    modalities: [],
    reasoningLevels: [],
    pricing: null,
    joinedFrom: null,
    capabilities: 'unknown',
    ...overrides,
  };
}

describe('formatContextWindow', () => {
  it('renders million- and thousand-token windows', () => {
    expect(formatContextWindow(1_000_000)).toBe('1M');
    expect(formatContextWindow(1_048_576)).toBe('1M');
    expect(formatContextWindow(131_072)).toBe('131K');
    expect(formatContextWindow(512)).toBe('512');
    expect(formatContextWindow(null)).toBeNull();
  });
});

describe('modelCapabilityChips', () => {
  it('shows verified thinking, multimodal, and window chips', () => {
    const chips = modelCapabilityChips(
      entry({
        id: 'gemini-3.5-flash',
        window: 1_048_576,
        modalities: ['audio', 'image', 'pdf', 'text', 'video'],
        reasoningLevels: ['minimal', 'low', 'medium', 'high'],
        joinedFrom: 'google/gemini-3.5-flash',
        capabilities: 'known',
      }),
    );
    expect(chips.map((chip) => chip.label)).toEqual([
      '思考',
      '图像',
      'PDF',
      '音频',
      '视频',
      '1M',
    ]);
  });

  it('puts reasoning levels on the thinking chip in the selected summary', () => {
    const chips = modelCapabilityChips(
      entry({
        id: 'deepseek-v4-pro',
        window: 1_000_000,
        modalities: ['text'],
        reasoningLevels: ['high', 'max'],
        joinedFrom: 'deepseek/deepseek-v4-pro',
        capabilities: 'known',
      }),
      { detail: true },
    );
    expect(chips.map((chip) => chip.label)).toEqual(['思考（high / max）', '1M']);
  });

  it('does not invent thinking or image chips from heuristic unknown rows', () => {
    expect(
      modelCapabilityChips(
        entry({
          id: 'deepseek-flash',
          window: 131_072,
          modalities: ['text'],
          reasoningLevels: [],
          capabilities: 'unknown',
        }),
      ),
    ).toEqual([
      expect.objectContaining({ key: 'unknown', label: '未识别' }),
    ]);
  });

  it('omits chips when capabilities was not reported', () => {
    expect(modelCapabilityChips({ id: 'custom-model' } as GatewayModelEntry)).toEqual([]);
    expect(modelCapabilityChips(null)).toEqual([]);
  });
});
