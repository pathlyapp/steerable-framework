import { useCallback, useEffect, useState } from 'react';
import { LuLoaderCircle, LuActivity } from 'react-icons/lu';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

/**
 * TelemetrySettingsPanel — W6-6 遥测合规化的桌面 collector 配置面板。
 *
 * 配置 OTLP/HTTP collector 地址与隐私档位,后端落 `settings_kv`
 * (key = telemetry_settings),每轮 CoreLoop 结束后按所选档位把该轮 trace
 * 导出到 collector(默认 metadata:只出结构/时延/状态,不出内容)。
 *
 * 隐私模型(与 framework otel.py 的 PrivacyMode 对齐):
 *   - 留空地址 = 关。一条 trace 都不出进程(默认)。
 *   - metadata = 只导出 span/事件的结构、时延、状态;payload 正文与自由属性丢弃。
 *   - full     = 导出(已脱敏的)payload 与属性,仅当 collector 可信时选。
 *
 * 数据自管理:挂载拉一次,保存后刷新。走 local-backend REST
 * (`/api/v2/local-settings/telemetry`),无专用 IPC channel。
 */

type PrivacyMode = 'metadata' | 'full';

interface TelemetrySettingsWire {
  endpoint?: string;
  privacyMode?: PrivacyMode;
  serviceName?: string;
}

export function TelemetrySettingsPanel() {
  const [endpoint, setEndpoint] = useState('');
  const [privacyMode, setPrivacyMode] = useState<PrivacyMode>('metadata');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchSettings = useCallback(async () => {
    if (!isElectron()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<TelemetrySettingsWire | null>({
        method: 'GET',
        path: '/api/v2/local-settings/telemetry',
      });
      setEndpoint(res?.endpoint ?? '');
      setPrivacyMode(res?.privacyMode === 'full' ? 'full' : 'metadata');
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
      const saved = await getElectronBridge()!.localBackend.request<TelemetrySettingsWire>({
        method: 'POST',
        path: '/api/v2/local-settings/telemetry',
        body: { endpoint: endpoint.trim(), privacyMode },
      });
      setEndpoint(saved.endpoint ?? '');
      setStatus(
        saved.endpoint
          ? `已保存 — 遥测开启(${saved.privacyMode === 'full' ? '完整(已脱敏)' : '仅元数据'})`
          : '已保存 — 未配置 collector 地址,遥测保持关闭',
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-3.5 space-y-3">
        <h4 className="text-xs font-semibold text-agent-foreground flex items-center gap-1.5">
          <LuActivity className="h-3.5 w-3.5 text-agent-muted-foreground" />
          OTLP Collector
        </h4>

        {loading ? (
          <div className="flex items-center gap-2 py-2 text-xs text-agent-muted-foreground">
            <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
            读取遥测设置...
          </div>
        ) : (
          <>
            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-agent-muted-foreground">
                Collector 地址(/v1/traces)
              </label>
              <input
                type="text"
                value={endpoint}
                onChange={(e) => setEndpoint(e.target.value)}
                placeholder="http://127.0.0.1:4318/v1/traces(留空 = 关闭遥测)"
                className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 font-mono text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-[11px] font-medium text-agent-muted-foreground">
                隐私档位
              </label>
              <div className="flex gap-2">
                {(
                  [
                    { value: 'metadata', label: '仅元数据', hint: '结构/时延/状态,不含内容' },
                    { value: 'full', label: '完整(已脱敏)', hint: '含 payload,密钥已打码' },
                  ] as const
                ).map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => setPrivacyMode(opt.value)}
                    className={`flex-1 rounded-agent-md border px-3 py-2 text-left transition-colors ${
                      privacyMode === opt.value
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

            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] text-agent-muted-foreground">
                无论哪一档,密钥/令牌在导出前都会被脱敏;留空地址则完全不导出。
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
