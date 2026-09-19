import { useCallback, useEffect, useState } from 'react';
import { LuLoaderCircle, LuSearch } from 'react-icons/lu';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

/**
 * Search backend settings. `ddg` registers `web_search` with no key.
 * Tavily still needs a key. Empty Tavily + OpenAI hosted search is handled
 * on spawn, not here. DuckDuckGo is never a silent fallback.
 *
 * Saved to settings_kv; injected as STEERABLE_WEB_SEARCH_PROVIDER / API_KEY
 * on the next sidecar spawn (restart the app after saving).
 */

type SearchProvider = 'ddg' | 'tavily';

interface WebSearchSettingsWire {
  provider?: SearchProvider;
  apiKey?: string;
}

function readProvider(raw: unknown, fallback: SearchProvider): SearchProvider {
  if (raw === 'ddg' || raw === 'tavily') return raw;
  return fallback;
}

export function WebSearchSettingsPanel() {
  const [provider, setProvider] = useState<SearchProvider>('tavily');
  const [apiKey, setApiKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSettings = useCallback(async () => {
    if (!isElectron()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<WebSearchSettingsWire | null>({
        method: 'GET',
        path: '/api/v2/local-settings/web-search',
      });
      setProvider(readProvider(res?.provider, 'tavily'));
      setApiKey(res?.apiKey ?? '');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchSettings();
  }, [fetchSettings]);

  const handleSave = async () => {
    if (!isElectron()) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const posted = provider;
      const saved = await getElectronBridge()!.localBackend.request<WebSearchSettingsWire>({
        method: 'POST',
        path: '/api/v2/local-settings/web-search',
        body: { provider: posted, apiKey: apiKey.trim() },
      });
      // Old main processes echo `{ apiKey }` only. Missing provider must not
      // snap the radio back to Tavily — keep what the user just saved.
      const nextProvider = readProvider(saved?.provider, posted);
      setProvider(nextProvider);
      setApiKey(saved?.apiKey ?? '');
      if (nextProvider === 'ddg') {
        setStatus('已保存 — 重启应用后 sidecar 会注册免费搜索（DuckDuckGo）');
      } else if (saved?.apiKey) {
        setStatus('已保存 — 重启应用后 sidecar 会注册 web_search');
      } else {
        setStatus('已保存 — 未配置则模型没有搜索工具（OpenAI 可用聊天凭证走托管搜索）');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-2.5 space-y-2">
        <h4 className="text-xs font-semibold text-agent-foreground flex items-center gap-1.5">
          <LuSearch className="h-3.5 w-3.5 text-agent-muted-foreground" />
          网络搜索
        </h4>

        {loading ? (
          <div className="flex items-center gap-2 py-2 text-xs text-agent-muted-foreground">
            <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
            读取搜索设置...
          </div>
        ) : (
          <>
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-agent-muted-foreground">
                搜索后端
              </label>
              <div className="flex gap-2">
                {(
                  [
                    { value: 'ddg', label: '免费', hint: 'DuckDuckGo，无需 Key' },
                    { value: 'tavily', label: 'Tavily', hint: '需 API Key，结果更稳' },
                  ] as const
                ).map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    data-testid={`web-search-provider-${opt.value}`}
                    onClick={() => setProvider(opt.value)}
                    className={`flex-1 rounded-agent-md border px-3 py-2 text-left transition-colors ${
                      provider === opt.value
                        ? 'border-agent-foreground/40 bg-agent-foreground/5'
                        : 'border-agent-border bg-agent-canvas hover:bg-agent-muted/40'
                    }`}
                  >
                    <div className="text-xs font-medium text-agent-foreground">{opt.label}</div>
                    <div className="text-[10px] text-agent-muted-foreground">{opt.hint}</div>
                  </button>
                ))}
              </div>
            </div>

            {provider === 'tavily' && (
              <div className="space-y-1.5">
                <label className="text-[11px] font-medium text-agent-muted-foreground">
                  Tavily API Key
                </label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="tvly-...（留空 = 不注册 Tavily）"
                  className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 font-mono text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                  autoComplete="off"
                />
              </div>
            )}

            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] text-agent-muted-foreground">
                {provider === 'ddg'
                  ? '免费后端走 DuckDuckGo 公开搜索页，无需 Key。质量与稳定性不如 Tavily；国内网络可能需要系统代理。Harbor 评测仍关闭网络工具。'
                  : 'GLM / OpenRouter / DeepSeek 需要这把钥；OpenAI（api.openai.com）可用聊天凭证走托管搜索，不必另配。Harbor 评测仍关闭网络工具。'}
              </p>
              <button
                type="button"
                onClick={handleSave}
                disabled={saving}
                className={`h-8 shrink-0 px-4 rounded-full text-xs font-medium transition-all ${
                  saving
                    ? 'bg-agent-muted text-agent-muted-foreground cursor-not-allowed'
                    : 'bg-agent-foreground text-agent-canvas hover:opacity-90'
                }`}
              >
                {saving ? <LuLoaderCircle className="h-3 w-3 animate-spin" /> : '保存'}
              </button>
            </div>

            {status && (
              <p className="text-[10px] text-agent-muted-foreground bg-agent-muted/10 px-2 py-1 rounded border border-agent-border/20">
                {status}
              </p>
            )}
          </>
        )}
      </div>

      {error && (
        <div className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
          {error}
        </div>
      )}
    </div>
  );
}
