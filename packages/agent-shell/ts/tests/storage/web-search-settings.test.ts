import { describe, expect, it } from 'vitest';
import {
  defaultSearchBaseUrl,
  hostedSearchAvailable,
  mergeWebSearchSettings,
  sidecarWebSearchEnv,
} from '../../src/storage/web-search-settings.js';

describe('mergeWebSearchSettings', () => {
  it('trims and drops an empty key and defaults the provider to tavily', () => {
    expect(mergeWebSearchSettings({ apiKey: '  tvly-k  ' })).toEqual({
      provider: 'tavily',
      apiKey: 'tvly-k',
    });
    expect(mergeWebSearchSettings({ apiKey: '   ' })).toEqual({
      provider: 'tavily',
      apiKey: undefined,
    });
    expect(mergeWebSearchSettings()).toEqual({ provider: 'tavily', apiKey: undefined });
  });

  it('keeps an explicit ddg provider without requiring a key', () => {
    expect(mergeWebSearchSettings({ provider: 'ddg' })).toEqual({
      provider: 'ddg',
      apiKey: undefined,
    });
  });
});

describe('hostedSearchAvailable', () => {
  it('is true only for OpenAI hosts', () => {
    expect(hostedSearchAvailable('https://api.openai.com/v1')).toBe(true);
    expect(hostedSearchAvailable('https://api.openai.com')).toBe(true);
    expect(hostedSearchAvailable('https://api.deepseek.com')).toBe(false);
    expect(hostedSearchAvailable('https://open.bigmodel.cn/api/paas/v4')).toBe(false);
    expect(hostedSearchAvailable('https://openrouter.ai/api/v1')).toBe(false);
    expect(hostedSearchAvailable('')).toBe(false);
    expect(hostedSearchAvailable(undefined)).toBe(false);
  });
});

describe('sidecarWebSearchEnv', () => {
  it('prefers process env over the stored settings key', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: { STEERABLE_WEB_SEARCH_API_KEY: 'from-env' },
        storedApiKey: 'tvly-stored',
        storedProvider: 'ddg',
        llmBaseUrl: 'https://api.openai.com/v1',
      }),
    ).toEqual({ STEERABLE_WEB_SEARCH_API_KEY: 'from-env' });
  });

  it('injects the stored key when process env is empty', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        storedApiKey: 'tvly-stored',
      }),
    ).toEqual({ STEERABLE_WEB_SEARCH_API_KEY: 'tvly-stored' });
  });

  it('does not register a search backend when nothing is configured', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        llmBaseUrl: 'https://open.bigmodel.cn/api/paas/v4',
      }),
    ).toEqual({});
  });

  it('sets provider=host for OpenAI when there is no search key', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        llmBaseUrl: 'https://api.openai.com/v1',
      }),
    ).toEqual({ STEERABLE_WEB_SEARCH_PROVIDER: 'host' });
  });

  it('injects provider=ddg without a key when the user chose the free backend', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        storedProvider: 'ddg',
        llmBaseUrl: 'https://api.deepseek.com',
      }),
    ).toEqual({ STEERABLE_WEB_SEARCH_PROVIDER: 'ddg' });
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        storedProvider: 'ddg',
        llmBaseUrl: 'https://api.openai.com/v1',
      }),
    ).toEqual({ STEERABLE_WEB_SEARCH_PROVIDER: 'ddg' });
  });

  it('does not silently fall back to ddg for an empty Tavily key', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: {},
        storedProvider: 'tavily',
        llmBaseUrl: 'https://api.deepseek.com',
      }),
    ).toEqual({});
  });

  it('leaves an operator-set provider alone', () => {
    expect(
      sidecarWebSearchEnv({
        processEnv: { STEERABLE_WEB_SEARCH_PROVIDER: 'tavily' },
        llmBaseUrl: 'https://api.openai.com/v1',
      }),
    ).toEqual({});
  });
});

describe('defaultSearchBaseUrl', () => {
  it('matches the sidecar defaults', () => {
    expect(defaultSearchBaseUrl('brave')).toBe('https://api.search.brave.com');
    expect(defaultSearchBaseUrl('ddg')).toBe('https://html.duckduckgo.com');
    expect(defaultSearchBaseUrl('tavily')).toBe('https://api.tavily.com');
  });
});
