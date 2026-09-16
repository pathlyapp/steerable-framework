import { describe, expect, it } from 'vitest';
import {
  DEFAULT_LLM_SETTINGS,
  EXPIRED_BAKED_API_KEY,
  OLLAMA_DEFAULTS,
  OPENAI_COMPAT_DEFAULTS,
  llmSettingsCarryExpiredBakedKey,
  llmSettingsMatchOllamaDefaults,
  mergeLlmSettings,
  sanitizeCompatOverrides,
  sanitizeLlmProvider,
  sanitizePresetsChoice,
  sidecarWireProvider,
  vendorModelsListUrl,
  type LlmSettings,
} from '../../src/storage/llm-settings';

describe('factory defaults ship no baked-in apiKey', () => {
  it('neither DEFAULT_LLM_SETTINGS nor the OpenAI-compat preset carries a key', () => {
    expect(DEFAULT_LLM_SETTINGS.apiKey).toBeUndefined();
    expect(OPENAI_COMPAT_DEFAULTS.apiKey).toBeUndefined();
  });
});

describe('llmSettingsCarryExpiredBakedKey', () => {
  it('matches saved settings still carrying the expired factory key', () => {
    expect(
      llmSettingsCarryExpiredBakedKey({
        ...OPENAI_COMPAT_DEFAULTS,
        apiKey: EXPIRED_BAKED_API_KEY,
      }),
    ).toBe(true);
  });

  it('does not match a user-provided key', () => {
    expect(
      llmSettingsCarryExpiredBakedKey({ ...OPENAI_COMPAT_DEFAULTS, apiKey: 'sk-user-own-key' }),
    ).toBe(false);
  });

  it('does not match empty / absent keys', () => {
    expect(llmSettingsCarryExpiredBakedKey(OPENAI_COMPAT_DEFAULTS)).toBe(false);
    expect(llmSettingsCarryExpiredBakedKey({ ...OPENAI_COMPAT_DEFAULTS, apiKey: '' })).toBe(false);
  });
});

describe('llmSettingsMatchOllamaDefaults', () => {
  it('matches a fresh, untouched Ollama-default install', () => {
    expect(llmSettingsMatchOllamaDefaults(OLLAMA_DEFAULTS)).toBe(true);
  });

  it('still matches when optional fields are simply absent (older saved rows)', () => {
    const bare: LlmSettings = { provider: 'ollama', model: 'llama3.1:8b', baseUrl: 'http://127.0.0.1:11434' };
    expect(llmSettingsMatchOllamaDefaults(bare)).toBe(true);
  });

  it('does not match once the user changes the model', () => {
    expect(
      llmSettingsMatchOllamaDefaults({ ...OLLAMA_DEFAULTS, model: 'qwen2.5:14b' }),
    ).toBe(false);
  });

  it('does not match once the user changes maxTotalTokens (previously the missed fingerprint field)', () => {
    // Regression: before maxTotalTokens was added to the fingerprint, a user
    // who customized *only* this field looked identical to a fresh install
    // and got silently migrated back to the DeepSeek preset on every boot.
    expect(
      llmSettingsMatchOllamaDefaults({ ...OLLAMA_DEFAULTS, maxTotalTokens: 20000 }),
    ).toBe(false);
  });

  it('does not match a non-Ollama provider', () => {
    expect(llmSettingsMatchOllamaDefaults(OPENAI_COMPAT_DEFAULTS)).toBe(false);
  });

  it('still matches installs that persisted the legacy factory temperature 0.3', () => {
    // The factory default switched from an explicit 0.3 to "auto" (field
    // absent); rows saved before that change carry 0.3 and must keep
    // migrating.
    expect(
      llmSettingsMatchOllamaDefaults({ ...OLLAMA_DEFAULTS, temperature: 0.3 }),
    ).toBe(true);
  });
});

describe('mergeLlmSettings', () => {
  it('merges Ollama settings against the Ollama preset, not the DeepSeek preset', () => {
    const incoming: LlmSettings = {
      provider: 'ollama',
      model: 'llama3.1:8b',
      baseUrl: 'http://127.0.0.1:11434',
    };
    const merged = mergeLlmSettings(incoming);
    // Regression: previously always merged against OPENAI_COMPAT_DEFAULTS,
    // so switching to Ollama without an explicit `apiKey` silently carried
    // the baked-in DeepSeek key over.
    expect(merged.apiKey).toBeUndefined();
    expect(merged.provider).toBe('ollama');
  });

  it('merges OpenAI-compat settings against the DeepSeek preset, which carries no baked-in apiKey', () => {
    const incoming: LlmSettings = { provider: 'openai-compat', model: 'deepseek-chat' };
    const merged = mergeLlmSettings(incoming);
    // 出厂预设不再打包内置 key（旧内置 key 已过期，且打包 = 公开泄露）：
    // 未显式填 key 时合并结果必须是没有 key，而不是悄悄带上一把死 key。
    expect(merged.apiKey).toBeUndefined();
    expect(OPENAI_COMPAT_DEFAULTS.apiKey).toBeUndefined();
  });

  it('lets an explicit apiKey override the preset for either provider', () => {
    const merged = mergeLlmSettings({
      provider: 'openai-compat',
      model: 'deepseek-chat',
      apiKey: 'sk-user-provided',
    });
    expect(merged.apiKey).toBe('sk-user-provided');
  });

  it('does not carry the DeepSeek key onto Anthropic / Google settings', () => {
    const merged = mergeLlmSettings({
      provider: 'anthropic',
      model: 'claude-sonnet-4-6',
    });
    expect(merged.apiKey).toBeUndefined();
    expect(merged.provider).toBe('anthropic');
  });

  it('fully replacing with DEFAULT_LLM_SETTINGS round-trips unchanged', () => {
    expect(mergeLlmSettings(DEFAULT_LLM_SETTINGS)).toEqual(DEFAULT_LLM_SETTINGS);
  });
});

describe('sanitizeCompatOverrides (W1.3.2)', () => {
  it('keeps only known keys with coerced types', () => {
    expect(
      sanitizeCompatOverrides({
        supportsTemperature: false,
        maxTokensField: 'max_completion_tokens',
        reasoningDeltaFields: ['reasoning_content', ' reasoning '],
        unknownFutureFlag: true,
        supportsUsageInStreaming: 'yes',
      }),
    ).toEqual({
      supportsTemperature: false,
      maxTokensField: 'max_completion_tokens',
      reasoningDeltaFields: ['reasoning_content', 'reasoning'],
    });
  });

  it('keeps echoEmptyReasoningForToolCalls (DeepSeek empty-reasoning round-trip)', () => {
    expect(sanitizeCompatOverrides({ echoEmptyReasoningForToolCalls: true })).toEqual({
      echoEmptyReasoningForToolCalls: true,
    });
    // Non-boolean is dropped like every other bool flag.
    expect(sanitizeCompatOverrides({ echoEmptyReasoningForToolCalls: 'yes' })).toBeUndefined();
  });

  it('returns undefined for empty / non-object / all-invalid input', () => {
    expect(sanitizeCompatOverrides(undefined)).toBeUndefined();
    expect(sanitizeCompatOverrides('x')).toBeUndefined();
    expect(sanitizeCompatOverrides({ unknown: 1 })).toBeUndefined();
    expect(sanitizeCompatOverrides({ reasoningDeltaFields: [] })).toBeUndefined();
  });

  it('round-trips through mergeLlmSettings without losing compat', () => {
    const merged = mergeLlmSettings({
      provider: 'openai-compat',
      model: 'kimi-k2.6',
      compat: { supportsTemperature: false },
    });
    expect(merged.compat).toEqual({ supportsTemperature: false });
  });
});

describe('factory defaults carry no explicit temperature (auto = vendor preset)', () => {
  it('neither provider preset pins a temperature', () => {
    expect(OPENAI_COMPAT_DEFAULTS.temperature).toBeUndefined();
    expect(OLLAMA_DEFAULTS.temperature).toBeUndefined();
  });

  it('an explicit temperature still survives the merge', () => {
    const merged = mergeLlmSettings({
      provider: 'openai-compat',
      model: 'deepseek-chat',
      temperature: 0.9,
    });
    expect(merged.temperature).toBe(0.9);
  });
});

describe('sanitizePresetsChoice', () => {
  it('keeps enabled and a typed override, dropping unknown keys', () => {
    expect(
      sanitizePresetsChoice({
        enabled: false,
        override: {
          temperature: 0.6,
          topP: 0.95,
          maxTokens: 8192,
          reasoningEffort: 'medium',
          extraBody: { top_k: 20 },
          bogusKey: 1,
        },
        unknownFutureKey: true,
      }),
    ).toEqual({
      enabled: false,
      override: {
        temperature: 0.6,
        topP: 0.95,
        maxTokens: 8192,
        reasoningEffort: 'medium',
        extraBody: { top_k: 20 },
      },
    });
  });

  it('returns undefined for empty / non-object / all-invalid input', () => {
    expect(sanitizePresetsChoice(undefined)).toBeUndefined();
    expect(sanitizePresetsChoice('auto')).toBeUndefined();
    expect(sanitizePresetsChoice({ unknown: 1 })).toBeUndefined();
    expect(sanitizePresetsChoice({ override: { bogus: 1 } })).toBeUndefined();
    expect(sanitizePresetsChoice({ override: 'x' })).toBeUndefined();
  });

  it('round-trips through mergeLlmSettings without losing presets', () => {
    const merged = mergeLlmSettings({
      provider: 'openai-compat',
      model: 'deepseek-chat',
      presets: { enabled: false },
    });
    expect(merged.presets).toEqual({ enabled: false });
  });
});

describe('sidecarWireProvider / sanitizeLlmProvider', () => {
  it('maps persisted providers onto sidecar factory kinds', () => {
    expect(sidecarWireProvider('openai-compat')).toBe('openai_compat');
    expect(sidecarWireProvider('ollama')).toBe('ollama');
    expect(sidecarWireProvider('anthropic')).toBe('anthropic');
    expect(sidecarWireProvider('google')).toBe('google');
    expect(sidecarWireProvider('openai-responses')).toBe('openai-responses');
  });

  it('coerces unknown persisted providers to openai-compat', () => {
    expect(sanitizeLlmProvider('nope')).toBe('openai-compat');
    expect(sanitizeLlmProvider('anthropic')).toBe('anthropic');
  });
});

describe('vendorModelsListUrl', () => {
  it('builds the vendor listing URL for each wire kind', () => {
    expect(vendorModelsListUrl('openai-compat', 'https://api.deepseek.com')).toBe(
      'https://api.deepseek.com/models',
    );
    expect(vendorModelsListUrl('anthropic', 'https://api.anthropic.com')).toBe(
      'https://api.anthropic.com/v1/models',
    );
    expect(vendorModelsListUrl('google', 'https://generativelanguage.googleapis.com')).toBe(
      'https://generativelanguage.googleapis.com/v1beta/models',
    );
    expect(vendorModelsListUrl('ollama', 'http://127.0.0.1:11434')).toBe(
      'http://127.0.0.1:11434/v1/models',
    );
  });
});
