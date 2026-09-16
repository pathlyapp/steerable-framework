import { describe, expect, it } from 'vitest';
import {
  defaultModelForVendor,
  FALLBACK_VENDORS,
  inferVendorId,
  llmProviderFromWireKind,
  mergeVendorOptions,
  modelPickerOptions,
  modelPickerRows,
  vendorLabel,
  type VendorOption,
} from './llm-vendors';
import type { LlmSettings } from '@/lib/local-api';

const vendors = mergeVendorOptions([
  {
    id: 'deepseek',
    apiBaseUrl: 'https://api.deepseek.com',
    envVars: [],
    wireKind: 'openai_compat',
    models: ['deepseek-v4-flash', 'deepseek-v4-pro'],
  },
  {
    id: 'anthropic',
    apiBaseUrl: 'https://api.anthropic.com',
    envVars: [],
    wireKind: 'anthropic',
    models: ['claude-sonnet-4-6'],
  },
  {
    id: 'azure',
    apiBaseUrl: null,
    envVars: ['AZURE_API_KEY'],
    wireKind: 'openai_compat',
    models: ['gpt-4o'],
  },
]);

describe('mergeVendorOptions', () => {
  it('pins featured vendors and injects ollama / custom', () => {
    const ids = vendors.map((v) => v.id);
    expect(ids.slice(0, 2)).toEqual(['deepseek', 'anthropic']);
    expect(ids).toContain('ollama');
    expect(ids).toContain('custom');
    expect(ids).toContain('azure');
    expect(vendorLabel('alibaba-cn')).toBe('阿里云百炼');
  });

  it('orders the fallback featured list by common usage', () => {
    expect(
      mergeVendorOptions(FALLBACK_VENDORS)
        .filter((v) => v.featured)
        .map((v) => v.id),
    ).toEqual([
      'deepseek',
      'alibaba-cn',
      'moonshotai-cn',
      'zhipuai',
      'openai',
      'anthropic',
      'google',
      'openrouter',
      'xai',
      'groq',
      'lmstudio',
      'ollama',
      'custom',
    ]);
  });
});

describe('inferVendorId', () => {
  it('prefers an explicit vendorId when that vendor is listed', () => {
    expect(
      inferVendorId({ provider: 'openai-compat', vendorId: 'anthropic', model: 'x' }, vendors),
    ).toBe('anthropic');
  });

  it('matches a saved DeepSeek URL back to the catalog vendor', () => {
    const settings: LlmSettings = {
      provider: 'openai-compat',
      model: 'deepseek-v4-flash',
      baseUrl: 'https://api.deepseek.com',
    };
    expect(inferVendorId(settings, vendors)).toBe('deepseek');
  });

  it('uses ollama for the local provider', () => {
    expect(
      inferVendorId(
        { provider: 'ollama', model: 'llama3.1:8b', baseUrl: 'http://127.0.0.1:11434' },
        vendors,
      ),
    ).toBe('ollama');
  });
});

describe('llmProviderFromWireKind', () => {
  it('maps sidecar kinds onto persisted provider ids', () => {
    expect(llmProviderFromWireKind('openai_compat')).toBe('openai-compat');
    expect(llmProviderFromWireKind('anthropic')).toBe('anthropic');
    expect(llmProviderFromWireKind('google')).toBe('google');
    expect(llmProviderFromWireKind('openai-responses')).toBe('openai-responses');
    expect(llmProviderFromWireKind('ollama')).toBe('ollama');
  });
});

describe('defaultModelForVendor', () => {
  it('keeps the current model when it is on the vendor list', () => {
    const vendor = vendors.find((v) => v.id === 'deepseek') as VendorOption;
    expect(defaultModelForVendor(vendor, 'deepseek-v4-pro')).toBe('deepseek-v4-pro');
  });

  it('falls back to the first catalog model', () => {
    const vendor = vendors.find((v) => v.id === 'deepseek') as VendorOption;
    expect(defaultModelForVendor(vendor, 'other')).toBe('deepseek-v4-flash');
  });
});

describe('modelPickerOptions', () => {
  const catalog = ['deepseek-chat', 'deepseek-v4-flash', 'deepseek-v4-pro'];

  it('uses the live gateway list and does not mix in catalog ids', () => {
    expect(
      modelPickerOptions(['deepseek-chat', 'deepseek-reasoner'], catalog, 'deepseek-chat', 'live'),
    ).toEqual(['deepseek-chat', 'deepseek-reasoner']);
  });

  it('keeps the currently selected id even if the gateway omitted it', () => {
    expect(
      modelPickerOptions(['deepseek-chat'], catalog, 'deepseek-v4-flash', 'live'),
    ).toEqual(['deepseek-chat', 'deepseek-v4-flash']);
  });

  it('falls back to the vendor catalog when the gateway listing is unavailable', () => {
    expect(modelPickerOptions([], catalog, 'deepseek-chat', 'offline')).toEqual(catalog);
  });
});

describe('modelPickerRows', () => {
  const catalog = ['deepseek-chat', 'deepseek-v4-flash'];

  it('keeps live capability rows and marks a missing current id as unknown', () => {
    const live = [
      {
        id: 'deepseek-v4-pro',
        name: 'DeepSeek V4 Pro',
        window: 1_000_000,
        modalities: ['text'],
        reasoningLevels: ['high', 'max'],
        pricing: null,
        joinedFrom: 'deepseek/deepseek-v4-pro',
        capabilities: 'known' as const,
      },
    ];
    expect(modelPickerRows(live, catalog, 'deepseek-v4-flash', 'live')).toEqual([
      { id: 'deepseek-v4-pro', entry: live[0] },
      {
        id: 'deepseek-v4-flash',
        entry: expect.objectContaining({ id: 'deepseek-v4-flash', capabilities: 'unknown' }),
      },
    ]);
  });

  it('does not invent capabilities for the vendor catalog fallback', () => {
    expect(modelPickerRows([], catalog, 'deepseek-chat', 'offline')).toEqual([
      { id: 'deepseek-chat', entry: null },
      { id: 'deepseek-v4-flash', entry: null },
    ]);
  });
});
