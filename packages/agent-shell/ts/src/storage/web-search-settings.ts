/**
 * Web search settings: Tavily key and/or an explicit free backend.
 *
 * Empty Tavily key → sidecar does not register `web_search` unless:
 * - stored provider is `ddg` (DuckDuckGo lite, no key), or
 * - the current LLM has hosted search (OpenAI) and the host injects
 *   `STEERABLE_WEB_SEARCH_PROVIDER=host`.
 * DuckDuckGo is never a silent fallback for an empty Tavily key.
 */

export type WebSearchProviderId = 'tavily' | 'ddg';

export interface WebSearchSettings {
  /** Search backend. Default `tavily` keeps the previous empty-key = off behavior. */
  provider: WebSearchProviderId;
  /** Search backend key (Tavily). Empty/undefined = not configured. */
  apiKey?: string;
}

export const DEFAULT_WEB_SEARCH_SETTINGS: WebSearchSettings = {
  provider: 'tavily',
  apiKey: undefined,
};

export function normalizeWebSearchProvider(raw: unknown): WebSearchProviderId {
  return raw === 'ddg' ? 'ddg' : 'tavily';
}

export function mergeWebSearchSettings(
  settings?: Partial<WebSearchSettings> | null,
): WebSearchSettings {
  const incoming = settings ?? {};
  const key =
    typeof incoming.apiKey === 'string' ? incoming.apiKey.trim() : '';
  return {
    provider: normalizeWebSearchProvider(incoming.provider),
    apiKey: key || undefined,
  };
}

/** Hostnames whose chat credential can run hosted web search (OpenAI). */
export function hostedSearchAvailable(baseUrl?: string): boolean {
  if (!baseUrl || !baseUrl.trim()) return false;
  try {
    const host = new URL(baseUrl.includes('://') ? baseUrl : `https://${baseUrl}`).hostname;
    return host === 'api.openai.com' || host.endsWith('.openai.com');
  } catch {
    return false;
  }
}

/**
 * Origin for the in-process search backend. Mirrors
 * `WebToolsConfig` / `_default_search_base_url` in the sidecar.
 */
export function defaultSearchBaseUrl(provider: string): string {
  if (provider === 'brave') return 'https://api.search.brave.com';
  if (provider === 'ddg') return 'https://html.duckduckgo.com';
  return 'https://api.tavily.com';
}

/**
 * Sidecar env the desktop injects at spawn. Process env wins over stored
 * settings. `ddg` is an explicit stored (or operator) choice. `host` is
 * only set when there is no search key, the stored provider is not `ddg`,
 * and the LLM base URL is OpenAI.
 */
export function sidecarWebSearchEnv(opts: {
  processEnv: NodeJS.ProcessEnv;
  storedApiKey?: string;
  storedProvider?: WebSearchProviderId;
  llmBaseUrl?: string;
}): Record<string, string> {
  const fromEnv = (
    (opts.processEnv.STEERABLE_WEB_SEARCH_API_KEY || '').trim()
    || (opts.processEnv.TAVILY_API_KEY || '').trim()
  );
  if (fromEnv) {
    return { STEERABLE_WEB_SEARCH_API_KEY: fromEnv };
  }
  if ((opts.processEnv.STEERABLE_WEB_SEARCH_PROVIDER || '').trim()) {
    return {};
  }
  if (opts.storedProvider === 'ddg') {
    return { STEERABLE_WEB_SEARCH_PROVIDER: 'ddg' };
  }
  const stored = (opts.storedApiKey || '').trim();
  if (stored) {
    return { STEERABLE_WEB_SEARCH_API_KEY: stored };
  }
  if (hostedSearchAvailable(opts.llmBaseUrl)) {
    return { STEERABLE_WEB_SEARCH_PROVIDER: 'host' };
  }
  return {};
}
