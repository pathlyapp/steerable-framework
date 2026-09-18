import { describe, expect, it, vi } from 'vitest';

import { LlmService } from '../src/llm/index.js';
import type { LlmSettings } from '../src/storage/index.js';

const initial: LlmSettings = {
  provider: 'openai',
  model: 'model-a',
  baseUrl: 'https://example.test',
  apiKey: '',
  temperature: 0.2,
};

describe('LlmService settings cache', () => {
  it('is unavailable before explicit storage-backed initialization', () => {
    expect(() => new LlmService().getSettings()).toThrow(
      'must be initialized after storage',
    );
  });

  it('replaces the cache only after persistence succeeds', async () => {
    const service = new LlmService();
    const setLlmSettings = vi
      .fn<(settings: LlmSettings) => Promise<LlmSettings>>()
      .mockRejectedValueOnce(new Error('storage unavailable'))
      .mockImplementationOnce(async (settings) => ({ ...settings, model: 'saved-model' }));
    service.initialize(initial, { setLlmSettings });
    const next = { ...initial, model: 'model-b' };

    await expect(service.setSettings(next)).rejects.toThrow('storage unavailable');
    expect(service.getSettings()).toEqual(initial);

    await expect(service.setSettings(next)).resolves.toEqual({
      ...next,
      model: 'saved-model',
    });
    expect(service.getSettings()).toEqual({ ...next, model: 'saved-model' });
  });
});
