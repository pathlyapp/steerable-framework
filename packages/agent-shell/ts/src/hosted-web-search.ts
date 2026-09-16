/**
 * OpenAI hosted web search using the existing chat credential.
 *
 * The sidecar must not hold the chat key under credential-broker mode.
 * This runs in the Electron host. Providers without hosted search
 * (GLM, OpenRouter, DeepSeek, Ollama) stay on the Tavily settings key.
 */

import type { LlmSettings } from './storage/llm-settings.js';
import { hostedSearchAvailable } from './storage/web-search-settings.js';

interface SearchHit {
  title: string;
  url: string;
  snippet: string;
}

export async function executeHostedWebSearch(
  query: string,
  maxResults: number,
  settings: LlmSettings,
): Promise<{ success: boolean; data?: Record<string, unknown>; error?: string }> {
  const q = query.trim();
  if (!q) {
    return { success: false, error: 'query is empty' };
  }
  if (!hostedSearchAvailable(settings.baseUrl)) {
    return {
      success: false,
      error:
        'this LLM provider has no hosted web search; set a Tavily key in settings',
    };
  }
  if (!settings.apiKey) {
    return { success: false, error: 'hosted web search needs the chat API key' };
  }
  const base = (settings.baseUrl || 'https://api.openai.com').replace(/\/+$/, '');
  const url = `${base}/chat/completions`;
  const cap = Math.min(Math.max(maxResults, 1), 20);
  let response: Response;
  try {
    response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${settings.apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: settings.model,
        messages: [{ role: 'user', content: q }],
        web_search_options: {},
      }),
    });
  } catch (err) {
    return {
      success: false,
      error: `hosted web search request failed: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
  if (!response.ok) {
    const text = await response.text().catch(() => '');
    return {
      success: false,
      error:
        `hosted web search returned HTTP ${response.status}: ${text.slice(0, 200)}` +
        (response.status === 400
          ? ' — this model may not support hosted search; set a Tavily key'
          : ''),
    };
  }
  const payload = (await response.json()) as {
    choices?: Array<{
      message?: {
        content?: string;
        annotations?: Array<{
          type?: string;
          url_citation?: { url?: string; title?: string };
        }>;
      };
    }>;
  };
  const message = payload.choices?.[0]?.message;
  const hits: SearchHit[] = [];
  for (const annotation of message?.annotations ?? []) {
    const cite = annotation.url_citation;
    if (cite?.url) {
      hits.push({
        title: cite.title || '',
        url: cite.url,
        snippet: '',
      });
    }
    if (hits.length >= cap) break;
  }
  const snippet = typeof message?.content === 'string' ? message.content : '';
  return {
    success: true,
    data: {
      query: q,
      result_count: hits.length,
      results: hits,
      snippet: hits.length ? undefined : snippet.slice(0, 2000),
    },
  };
}
