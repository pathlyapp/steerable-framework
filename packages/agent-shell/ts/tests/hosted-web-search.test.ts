import { afterEach, describe, expect, it, vi } from 'vitest';
import { executeHostedWebSearch } from '../src/hosted-web-search.js';
import type { LlmSettings } from '../src/storage/llm-settings.js';

const openaiSettings: LlmSettings = {
  provider: 'openai-compat',
  model: 'gpt-4o-mini',
  baseUrl: 'https://api.openai.com/v1',
  apiKey: 'sk-test',
  temperature: 0.3,
};

describe('executeHostedWebSearch', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('refuses a non-OpenAI provider instead of pretending to search', async () => {
    const out = await executeHostedWebSearch('q', 5, {
      ...openaiSettings,
      baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    });
    expect(out.success).toBe(false);
    expect(out.error).toMatch(/Tavily/);
  });

  it('maps url_citation annotations to results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            choices: [
              {
                message: {
                  content: 'summary',
                  annotations: [
                    {
                      type: 'url_citation',
                      url_citation: { url: 'https://example.com/a', title: 'A' },
                    },
                  ],
                },
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    );
    const out = await executeHostedWebSearch('kv cache', 8, openaiSettings);
    expect(out.success).toBe(true);
    expect(out.data).toMatchObject({
      query: 'kv cache',
      result_count: 1,
      results: [{ title: 'A', url: 'https://example.com/a', snippet: '' }],
    });
  });
});
