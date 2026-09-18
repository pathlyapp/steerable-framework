import { useCallback, useEffect, useState } from 'react';
import { LuChartBar } from 'react-icons/lu';
import { BRAND_NAME } from '@/brand';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

type InsightsWire = {
  installId: string;
  shareBehavior: boolean;
  shareConversation: boolean;
  shareProfile: boolean;
  promptedAt?: string;
  apiBase?: string;
  profile: {
    displayName: string;
    email: string;
    company: string;
    note: string;
  };
  stats?: { events: number; turns: number; profile: number; pending: number };
};

const empty: InsightsWire = {
  installId: '',
  shareBehavior: false,
  shareConversation: false,
  shareProfile: false,
  profile: { displayName: '', email: '', company: '', note: '' },
  stats: { events: 0, turns: 0, profile: 0, pending: 0 },
};

export function InsightsConsentBanner() {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!isElectron()) return;
    void getElectronBridge()!
      .localBackend.request<InsightsWire>({ method: 'GET', path: '/api/v2/local-settings/insights' })
      .then((data) => {
        if (!data.promptedAt) setOpen(true);
      })
      .catch(() => {});
  }, []);

  const save = useCallback(async (upload: boolean) => {
    if (!isElectron()) return;
    setSaving(true);
    try {
      await getElectronBridge()!.localBackend.request({
        method: 'POST',
        path: '/api/v2/local-settings/insights',
        body: {
          shareBehavior: upload,
          shareConversation: upload,
          shareProfile: upload,
          markPrompted: true,
        },
      });
      setOpen(false);
    } catch {
      setOpen(false);
    } finally {
      setSaving(false);
    }
  }, []);

  if (!open) return null;

  return (
    <div
      className="flex-shrink-0 border-t border-agent-border bg-agent-muted px-4 py-3"
      data-testid="insights-consent-banner"
      role="region"
      aria-label="帮助改进产品同意"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-agent-foreground">帮助改进{BRAND_NAME}</p>
          <p className="mt-1 text-xs leading-relaxed text-agent-muted-foreground">
            同意上传数据到服务器，帮助改进产品。之后可在设置里更改。
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            disabled={saving}
            onClick={() => void save(true)}
            className="h-8 whitespace-nowrap rounded-full bg-agent-foreground px-3.5 text-xs text-agent-canvas disabled:opacity-50"
          >
            保存
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => void save(false)}
            className="h-8 whitespace-nowrap rounded-full border border-agent-border bg-agent-canvas px-3.5 text-xs text-agent-foreground disabled:opacity-50"
          >
            取消
          </button>
        </div>
      </div>
    </div>
  );
}

export function InsightsSettingsPanel() {
  const [data, setData] = useState<InsightsWire>(empty);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!isElectron()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<InsightsWire>({
        method: 'GET',
        path: '/api/v2/local-settings/insights',
      });
      setData({ ...empty, ...res, profile: { ...empty.profile, ...res.profile } });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!isElectron()) return;
    setSaving(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<InsightsWire>({
        method: 'POST',
        path: '/api/v2/local-settings/insights',
        body: {
          shareBehavior: data.shareBehavior,
          shareConversation: data.shareConversation,
          shareProfile: data.shareProfile,
          markPrompted: true,
          apiBase: data.apiBase ?? '',
          displayName: data.profile.displayName,
          email: data.profile.email,
          company: data.profile.company,
          note: data.profile.note,
        },
      });
      setData({ ...empty, ...res, profile: { ...empty.profile, ...res.profile } });
      setStatus('已保存。未勾选的种类只留在本机。');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const exportFile = async () => {
    if (!isElectron()) return;
    setError(null);
    try {
      const bundle = await getElectronBridge()!.localBackend.request<unknown>({
        method: 'GET',
        path: '/api/v2/insights/export',
      });
      const text = `${JSON.stringify(bundle, null, 2)}\n`;
      const saved = await getElectronBridge()!.local?.saveTextFile?.({
        title: '导出本地洞察记录',
        defaultPath: `deeppath-insights-${new Date().toISOString().slice(0, 10)}.json`,
        content: text,
      });
      if (saved?.canceled === false) setStatus(`已保存到 ${saved.filePath}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const uploadNow = async () => {
    if (!isElectron()) return;
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{ ok: boolean; detail: string }>({
        method: 'POST',
        path: '/api/v2/insights/upload-local',
      });
      setStatus(res.ok ? `已上传到${BRAND_NAME}服务器` : '上传失败，记录仍在本机，可改导出文件发送');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const stats = data.stats ?? empty.stats!;

  return (
    <div className="space-y-4" data-testid="insights-settings-panel">
      <div className="rounded-agent-md border border-agent-border/60 bg-agent-muted/30 p-3.5 space-y-3">
        <h4 className="flex items-center gap-1.5 text-xs font-semibold text-agent-foreground">
          <LuChartBar className="h-3.5 w-3.5 text-agent-muted-foreground" />
          帮助改进产品（行为 / 对话 / 用户信息 分开确认）
        </h4>
        <p className="text-[11px] text-agent-muted-foreground">
          即使用户不同意上传，记录也会留在本机。可导出 JSON 发给产品团队，或点一次「现在上传」。
        </p>
        {!isElectron() ? (
          <p className="text-[11px] text-agent-muted-foreground">需要在桌面客户端中打开</p>
        ) : loading ? (
          <p className="text-[11px] text-agent-muted-foreground">读取中…</p>
        ) : (
          <>
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={data.shareBehavior}
                onChange={(e) => setData((d) => ({ ...d, shareBehavior: e.target.checked }))}
              />
              <span>
                自动上传<strong>行为</strong>
                <span className="block text-[10px] text-agent-muted-foreground">打开、发送、设置卡住等，不含问答正文</span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={data.shareConversation}
                onChange={(e) => setData((d) => ({ ...d, shareConversation: e.target.checked }))}
              />
              <span>
                自动上传<strong>对话</strong>
                <span className="block text-[10px] text-agent-muted-foreground">提问与回答（已脱敏密钥和用户目录）</span>
              </span>
            </label>
            <label className="flex items-start gap-2 text-xs">
              <input
                type="checkbox"
                checked={data.shareProfile}
                onChange={(e) => setData((d) => ({ ...d, shareProfile: e.target.checked }))}
              />
              <span>
                自动上传<strong>用户信息</strong>
                <span className="block text-[10px] text-agent-muted-foreground">与上面两项分开，不勾选则称呼/邮箱只留本机</span>
              </span>
            </label>
            <div className="grid grid-cols-2 gap-2">
              {(
                [
                  ['displayName', '称呼'],
                  ['email', '邮箱'],
                  ['company', '公司/团队'],
                  ['note', '想告诉我们的'],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="space-y-1 text-[11px] text-agent-muted-foreground">
                  {label}
                  <input
                    value={data.profile[key]}
                    onChange={(e) =>
                      setData((d) => ({ ...d, profile: { ...d.profile, [key]: e.target.value } }))
                    }
                    className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground"
                  />
                </label>
              ))}
            </div>
            <p className="text-[10px] text-agent-muted-foreground">
              本机已记 行为 {stats.events} / 对话 {stats.turns} / 资料 {stats.profile}，待上传 {stats.pending}
              {data.installId ? ` · ${data.installId.slice(0, 8)}` : ''}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={saving}
                onClick={() => void save()}
                className="h-8 rounded-full bg-agent-foreground px-4 text-xs text-agent-canvas"
              >
                保存
              </button>
              <button
                type="button"
                onClick={() => void exportFile()}
                className="h-8 rounded-full border border-agent-border px-4 text-xs"
              >
                导出文件发给开发者
              </button>
              <button
                type="button"
                onClick={() => void uploadNow()}
                className="h-8 rounded-full border border-agent-border px-4 text-xs"
              >
                现在上传本地记录
              </button>
            </div>
          </>
        )}
        {status && <p className="text-[10px] text-agent-muted-foreground">{status}</p>}
      </div>
      {error && (
        <div className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
          {error}
        </div>
      )}
    </div>
  );
}
