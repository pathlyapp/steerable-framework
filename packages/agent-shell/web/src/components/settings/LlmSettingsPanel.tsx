import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import {
  LuLoaderCircle,
  LuRefreshCw,
} from 'react-icons/lu';
import {
  getLlmSettings,
  setLlmSettings,
  getCompatFlags,
  getProviderPresets,
  getCatalogProviders,
  getLlmModels,
  resolveProviderPreset,
  type CompatFlagDescriptor,
  type GatewayModelEntry,
  type LlmSettings,
  type ProviderPresetDescriptor,
  type ProviderPresetOverride,
} from '@/lib/local-api';
import {
  FALLBACK_VENDORS,
  defaultModelForVendor,
  inferVendorId,
  llmProviderFromWireKind,
  mergeVendorOptions,
  modelPickerRows,
  usesOpenAiCompatExtras,
  type LiveCatalogStatus,
  type VendorOption,
} from '@/components/settings/llm-vendors';
import { ModelCapabilityChips } from '@/components/settings/ModelCapabilityChips';
import { modelCapabilityChips } from '@/components/settings/model-capabilities';
import { ModelIdCombobox } from '@/components/settings/ModelIdCombobox';
import { SettingsSaveButton } from '@/components/settings/SettingsSaveButton';
import { isElectron } from '@/lib/electron-bridge';
import {
  COMPAT_AUTO,
  formStateFromOverrides,
  overridesFromFormState,
  type CompatFormState,
} from '@/components/settings/compat-flags-model';
import {
  choiceFromPresetMode,
  descriptorLabel,
  overrideFromDescriptor,
  overridesEqual,
  presetModeFromChoice,
  summarizePreset,
  type PresetMode,
} from '@/components/settings/preset-choice-model';

export interface LlmSettingsPanelHandle {
  save: () => Promise<void>;
}

export interface LlmSaveUi {
  saving: boolean;
  savedOk: boolean;
  loading: boolean;
}

interface LlmSettingsPanelProps {
  onSaved?: (settings: LlmSettings) => void;
  onSaveUiChange?: (ui: LlmSaveUi) => void;
  showFooterSave?: boolean;
}

// ─── 厂商参数预制（框架 llm.presets）────────────────────────────────────────
// 注册表与匹配规则都在框架侧（presets.describe / presets.resolve RPC），这里
// 只渲染与持久化选择；表单态转换在 settings/preset-choice-model.ts（可单测）。

/**
 * LlmSettingsPanel — 本地模型设置表单（服务商 / URL / Key / 模型 / 预制 / 超时）。
 *
 * 两处复用：设置页「设置」分区，以及输入区齿轮打开的 LocalLlmSettingsModal。
 * 挂载时自取当前配置；保存走 local-api `setLlmSettings`。
 */
export const LlmSettingsPanel = forwardRef<LlmSettingsPanelHandle, LlmSettingsPanelProps>(
  function LlmSettingsPanel(
    { onSaved, onSaveUiChange, showFooterSave = true },
    ref,
  ) {
  const [settings, setSettings] = useState<LlmSettings>({
    provider: 'openai-compat',
    vendorId: 'deepseek',
    model: 'deepseek-chat',
    baseUrl: 'https://api.deepseek.com',
    apiKey: '',
    // temperature 缺省 = 自动（命中厂商预制用预制值，否则不下发）。
    maxTotalTokens: 60000,
  });
  const [vendors, setVendors] = useState<VendorOption[]>(() => mergeVendorOptions(FALLBACK_VENDORS));
  const [liveEntries, setLiveEntries] = useState<GatewayModelEntry[]>([]);
  const [liveCatalogStatus, setLiveCatalogStatus] = useState<LiveCatalogStatus>('idle');
  const [modelsRefreshing, setModelsRefreshing] = useState(false);
  const [keyTest, setKeyTest] = useState<
    | { status: 'idle' }
    | { status: 'testing' }
    | { status: 'ok'; count: number }
    | { status: 'fail'; detail: string }
  >({ status: 'idle' });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedOk, setSavedOk] = useState(false);
  const savedOkTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 超时设置（秒）。字符串存储以支持"留空 = 用默认值"。
  // execTimeoutSeconds 随 LlmSettings 持久化；场景包的超时项由包自带的
  // 设置面板承载（3.1 起 shell 设置页无包内联区块）。
  const [execTimeoutInput, setExecTimeoutInput] = useState('');

  // W1.3.2 compat 旗标：词汇表由 sidecar compat.describe 服务化（框架是
  // 单一真源）；表单三态字符串，'auto' = 不覆盖、走框架 URL 自动探测。
  const [compatFlags, setCompatFlags] = useState<CompatFlagDescriptor[]>([]);
  const [compatForm, setCompatForm] = useState<CompatFormState>({});

  // 厂商参数预制：注册表由 sidecar presets.describe 服务化；resolvedPreset
  // 是「自动」档下当前 baseUrl+model 的命中预览（presets.resolve）。
  const [presetMode, setPresetMode] = useState<PresetMode>('auto');
  const [presetList, setPresetList] = useState<ProviderPresetDescriptor[]>([]);
  const [presetPinnedIdx, setPresetPinnedIdx] = useState(-1);
  const [resolvedPreset, setResolvedPreset] = useState<ProviderPresetOverride | null>(null);

  const reload = useCallback(async () => {
    if (!isElectron()) {
      setError('需要在桌面客户端中打开');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await getLlmSettings();
      let nextVendors = mergeVendorOptions(FALLBACK_VENDORS);
      try {
        const { providers } = await getCatalogProviders();
        if (providers.length > 0) nextVendors = mergeVendorOptions(providers);
      } catch (err) {
        console.warn('读取服务商目录失败:', err);
      }
      setVendors(nextVendors);
      const vendorId = inferVendorId(data, nextVendors);
      setSettings((prev) => ({
        ...prev,
        ...data,
        vendorId,
        baseUrl: data.baseUrl || '',
        apiKey: data.apiKey || '',
      }));
      // compat 旗标词汇表：sidecar 未就绪时留空（设置区显示提示，不阻塞
      // 其余设置项）。表单初值以已持久化的覆盖为准。
      try {
        const { flags } = await getCompatFlags();
        setCompatFlags(flags);
        setCompatForm(formStateFromOverrides(data.compat, flags));
      } catch (err) {
        console.warn('读取 compat 旗标词汇表失败:', err);
        setCompatFlags([]);
        setCompatForm({});
      }
      // 预制注册表：同样服务化；初值以已持久化的选择为准（钉死的 override
      // 若在注册表里找到同参数行则选中该行，否则按「自定义」展示）。
      try {
        const { presets } = await getProviderPresets();
        setPresetList(presets);
        const mode = presetModeFromChoice(data.presets);
        setPresetMode(mode);
        if (mode === 'pinned' && data.presets?.override) {
          const idx = presets.findIndex((d) =>
            overridesEqual(overrideFromDescriptor(d), data.presets!.override!),
          );
          setPresetPinnedIdx(idx);
        } else {
          setPresetPinnedIdx(-1);
        }
      } catch (err) {
        console.warn('读取厂商预制注册表失败:', err);
        setPresetList([]);
        setPresetMode(presetModeFromChoice(data.presets));
      }
      setExecTimeoutInput(
        data.execTimeoutSeconds && data.execTimeoutSeconds > 0
          ? String(data.execTimeoutSeconds)
          : '',
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(
    () => () => {
      if (savedOkTimerRef.current) clearTimeout(savedOkTimerRef.current);
    },
    [],
  );

  // 「自动」档的命中预览：baseUrl/model 变化后向框架求一次 resolve
  // （防抖 300ms；sidecar 未就绪时静默留空，预览区显示提示）。
  useEffect(() => {
    if (!usesOpenAiCompatExtras(settings.provider) || presetMode !== 'auto') return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      resolveProviderPreset(settings.baseUrl || undefined, settings.model || undefined)
        .then(({ preset }) => {
          if (!cancelled) setResolvedPreset(preset);
        })
        .catch(() => {
          if (!cancelled) setResolvedPreset(null);
        });
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [settings.provider, settings.baseUrl, settings.model, presetMode]);

  const selectedVendor =
    vendors.find((v) => v.id === (settings.vendorId || inferVendorId(settings, vendors))) ??
    vendors.find((v) => v.id === 'custom') ??
    vendors[0];

  const catalogModels = selectedVendor?.models ?? [];
  const modelOptions = modelPickerRows(
    liveEntries,
    catalogModels,
    settings.model,
    liveCatalogStatus,
  );
  const selectedEntry = liveEntries.find((entry) => entry.id === settings.model) ?? null;
  const selectedKnownEmpty =
    selectedEntry?.capabilities === 'known' &&
    modelCapabilityChips(selectedEntry, { detail: true }).length === 0;

  const featuredVendors = vendors.filter((v) => v.featured);
  const otherVendors = vendors.filter((v) => !v.featured);

  useEffect(() => {
    const baseUrl = settings.baseUrl?.trim();
    if (!baseUrl) {
      setLiveEntries([]);
      setLiveCatalogStatus('idle');
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      getLlmModels({
        baseUrl,
        apiKey: settings.apiKey || undefined,
        provider: settings.provider,
      })
        .then((catalog) => {
          if (cancelled) return;
          setLiveEntries(catalog.models);
          setLiveCatalogStatus(catalog.catalogStatus);
        })
        .catch(() => {
          if (cancelled) return;
          setLiveEntries([]);
          setLiveCatalogStatus('offline');
        });
    }, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [settings.baseUrl, settings.apiKey, settings.provider]);

  useEffect(() => {
    setKeyTest({ status: 'idle' });
  }, [settings.baseUrl, settings.apiKey, settings.provider]);

  const handleSwitchVendor = (vendorId: string) => {
    const vendor = vendors.find((v) => v.id === vendorId);
    if (!vendor) return;
    setLiveEntries([]);
    setLiveCatalogStatus('idle');
    setSettings((prev) => ({
      ...prev,
      vendorId: vendor.id,
      provider: llmProviderFromWireKind(vendor.wireKind),
      baseUrl: vendor.apiBaseUrl ?? '',
      model: defaultModelForVendor(vendor, prev.model),
    }));
  };

  const handleRefreshModels = async () => {
    const baseUrl = settings.baseUrl?.trim();
    if (!baseUrl || modelsRefreshing) return;
    setModelsRefreshing(true);
    try {
      const catalog = await getLlmModels({
        baseUrl,
        apiKey: settings.apiKey || undefined,
        provider: settings.provider,
        refresh: true,
      });
      setLiveEntries(catalog.models);
      setLiveCatalogStatus(catalog.catalogStatus);
    } catch {
      setLiveEntries([]);
      setLiveCatalogStatus('offline');
    } finally {
      setModelsRefreshing(false);
    }
  };

  const handleTestKey = async () => {
    const baseUrl = settings.baseUrl?.trim();
    if (!baseUrl || keyTest.status === 'testing') return;
    setKeyTest({ status: 'testing' });
    try {
      const catalog = await getLlmModels({
        baseUrl,
        apiKey: settings.apiKey || undefined,
        provider: settings.provider,
        refresh: true,
      });
      setLiveEntries(catalog.models);
      setLiveCatalogStatus(catalog.catalogStatus);
      if (catalog.catalogStatus === 'offline') {
        setKeyTest({
          status: 'fail',
          detail: catalog.error || '网关目录不可用',
        });
        return;
      }
      setKeyTest({ status: 'ok', count: catalog.models.length });
    } catch (err) {
      setLiveEntries([]);
      setLiveCatalogStatus('offline');
      setKeyTest({
        status: 'fail',
        detail: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const handleSave = async () => {
    if (!isElectron()) {
      setError('需要在桌面客户端中打开');
      return;
    }
    setSaving(true);
    setError(null);
    setSavedOk(false);
    try {
      const parseSeconds = (raw: string): number | undefined => {
        const n = parseInt(raw.trim(), 10);
        return Number.isFinite(n) && n > 0 ? n : undefined;
      };
      const execTimeoutSeconds = parseSeconds(execTimeoutInput);
      const saved = await setLlmSettings({
        provider: settings.provider,
        vendorId: settings.vendorId,
        model: settings.model,
        baseUrl: settings.baseUrl?.trim() || undefined,
        apiKey: settings.apiKey?.trim() || undefined,
        temperature: settings.temperature,
        systemPrompt: settings.systemPrompt?.trim() || undefined,
        maxTotalTokens: settings.maxTotalTokens,
        execTimeoutSeconds,
        compat:
          settings.provider === 'openai-compat'
            ? overridesFromFormState(compatForm, compatFlags)
            : undefined,
        presets: usesOpenAiCompatExtras(settings.provider)
          ? choiceFromPresetMode(presetMode, presetPinnedIdx, presetList, settings.presets)
          : undefined,
      });
      setSettings((prev) => ({ ...prev, ...saved }));
      setSavedOk(true);
      onSaved?.(saved);
      if (savedOkTimerRef.current) clearTimeout(savedOkTimerRef.current);
      savedOkTimerRef.current = setTimeout(() => setSavedOk(false), 1500);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleSaveRef = useRef(handleSave);
  handleSaveRef.current = handleSave;
  useImperativeHandle(ref, () => ({
    save: () => handleSaveRef.current(),
  }), []);

  useEffect(() => {
    onSaveUiChange?.({ saving, savedOk, loading });
  }, [saving, savedOk, loading, onSaveUiChange]);

  return (
    <div className="space-y-3">
          {loading ? (
            <div className="flex items-center gap-2 py-2 text-xs text-agent-muted-foreground">
              <LuLoaderCircle className="h-4 w-4 animate-spin" />
              加载当前配置...
            </div>
          ) : (
            <>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                    服务商
                  </label>
                  <select
                    data-testid="llm-vendor-select"
                    value={selectedVendor?.id ?? 'custom'}
                    onChange={(e) => handleSwitchVendor(e.target.value)}
                    className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                  >
                    {featuredVendors.length > 0 && (
                      <optgroup label="常用">
                        {featuredVendors.map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.label}
                          </option>
                        ))}
                      </optgroup>
                    )}
                    {otherVendors.length > 0 && (
                      <optgroup label="全部">
                        {otherVendors.map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.label}
                          </option>
                        ))}
                      </optgroup>
                    )}
                  </select>
                  <p className="mt-1.5 text-[11px] text-agent-muted-foreground">
                    {selectedVendor?.id === 'ollama'
                      ? '请确认本机已运行 `ollama serve` 并且执行过 `ollama pull <model>`。'
                      : selectedVendor?.id === 'custom'
                        ? '任意兼容 OpenAI Chat Completions 的网关。Anthropic / Gemini 请从上方列表选择对应服务商。'
                        : selectedVendor?.wireKind === 'anthropic'
                          ? '走 Anthropic 原生 Messages 协议。'
                          : selectedVendor?.wireKind === 'google'
                            ? '走 Google Gemini 原生协议。'
                            : selectedVendor?.wireKind === 'openai-responses'
                              ? '走 OpenAI Responses API（如 xAI Grok）。'
                              : '兼容 OpenAI Chat Completions 的云端或本地网关。'}
                  </p>
                </div>

                <div>
                  <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                    Base URL
                  </label>
                  <input
                    type="text"
                    value={settings.baseUrl || ''}
                    onChange={(e) =>
                      setSettings((prev) => ({ ...prev, baseUrl: e.target.value }))
                    }
                    placeholder={
                      selectedVendor?.apiBaseUrl ||
                      (selectedVendor?.id === 'custom'
                        ? 'https://your-gateway.example/v1'
                        : '该服务商没有默认地址，请手动填写')
                    }
                    className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                  />
                  <p className="mt-1.5 text-[11px] text-agent-muted-foreground">
                    {selectedVendor?.apiBaseUrl
                      ? '已按服务商填入默认地址，私有部署可改。'
                      : '目录里没有默认 URL，需要手动填写。'}
                  </p>
                </div>

                {settings.provider !== 'ollama' && (
                  <div>
                    <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                      API Key
                    </label>
                    <div className="flex items-center gap-2">
                      <input
                        type="password"
                        value={settings.apiKey || ''}
                        onChange={(e) =>
                          setSettings((prev) => ({ ...prev, apiKey: e.target.value }))
                        }
                        placeholder={
                          selectedVendor?.id === 'custom'
                            ? 'sk-... (可选，私有部署可留空)'
                            : 'sk-... (必填，到服务商控制台创建)'
                        }
                        className="h-8 min-w-0 flex-1 rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                      />
                      <button
                        type="button"
                        data-testid="llm-key-test"
                        title="用当前 URL 和 Key 向网关拉一次模型目录"
                        disabled={!settings.baseUrl?.trim() || keyTest.status === 'testing'}
                        onClick={() => void handleTestKey()}
                        className="inline-flex h-8 shrink-0 items-center justify-center rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs font-medium text-agent-foreground transition-colors hover:bg-agent-foreground/5 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {keyTest.status === 'testing' ? (
                          <LuLoaderCircle className="h-4 w-4 animate-spin" />
                        ) : (
                          '测试'
                        )}
                      </button>
                    </div>
                    <p
                      className={[
                        'mt-1.5 text-[11px]',
                        keyTest.status === 'fail'
                          ? 'text-agent-destructive'
                          : keyTest.status === 'idle' && !settings.apiKey?.trim()
                            ? 'text-amber-600'
                            : 'text-agent-muted-foreground',
                      ].join(' ')}
                    >
                      {keyTest.status === 'testing'
                        ? '正在验证凭证…'
                        : keyTest.status === 'ok'
                          ? `凭证可用，网关返回 ${keyTest.count} 个模型。`
                          : keyTest.status === 'fail'
                            ? `验证失败：${keyTest.detail}`
                            : !settings.apiKey?.trim()
                              ? '尚未配置 API Key：到服务商控制台创建密钥（DeepSeek：platform.deepseek.com → API Keys），粘贴到上方输入框，点「测试」验证通过后保存。'
                              : '网络搜索在设置页配置：可选用免费搜索或 Tavily 钥；OpenAI 可用这把聊天钥走托管搜索。'}
                    </p>
                  </div>
                )}

                <div>
                  <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                    模型
                  </label>
                  <div className="flex items-center gap-2">
                    <div className="min-w-0 flex-1">
                      <ModelIdCombobox
                        value={settings.model}
                        options={modelOptions}
                        placeholder={liveEntries[0]?.id || catalogModels[0] || '选择或填写模型 id'}
                        onChange={(model) => setSettings((prev) => ({ ...prev, model }))}
                      />
                    </div>
                    <button
                      type="button"
                      data-testid="llm-model-refresh"
                      title="从网关重新拉取模型列表"
                      aria-label="刷新模型目录"
                      disabled={!settings.baseUrl?.trim() || modelsRefreshing}
                      onClick={() => void handleRefreshModels()}
                      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-agent-md border border-agent-border bg-agent-canvas text-agent-muted-foreground transition-colors hover:bg-agent-foreground/5 hover:text-agent-foreground disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <LuRefreshCw className={`h-4 w-4 ${modelsRefreshing ? 'animate-spin' : ''}`} />
                    </button>
                  </div>
                  {(liveCatalogStatus === 'live' || liveCatalogStatus === 'stale') && (
                    <div
                      data-testid="llm-model-capabilities"
                      className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-agent-muted-foreground"
                    >
                      <span>当前</span>
                      {selectedEntry ? (
                        selectedKnownEmpty ? (
                          <span>文本对话</span>
                        ) : (
                          <ModelCapabilityChips entry={selectedEntry} detail />
                        )
                      ) : (
                        <span>能力未识别（不在网关目录）</span>
                      )}
                    </div>
                  )}
                  <p className="mt-1.5 text-[11px] text-agent-muted-foreground">
                    {liveCatalogStatus === 'live'
                      ? `已从网关拉取 ${liveEntries.length} 个模型，也可直接填写。`
                      : liveCatalogStatus === 'stale'
                        ? '网关目录刷新失败，正在用上次拉取的缓存；也可直接填写。'
                        : liveCatalogStatus === 'idle' && (settings.baseUrl || '').trim()
                          ? '正在从网关拉取模型目录…'
                          : liveCatalogStatus === 'idle'
                            ? '填写 Base URL 后可拉取网关模型目录，也可直接填写。'
                            : catalogModels.length > 0
                              ? '网关目录不可用，暂用服务商内置列表；也可直接填写。'
                              : '网关目录不可用，请直接填写模型 id。'}
                  </p>
                </div>

                {usesOpenAiCompatExtras(settings.provider) && (
                  <div className="rounded-agent-md border border-agent-border/60 p-3 space-y-3">
                    <div>
                      <h4 className="text-xs font-semibold text-agent-muted-foreground uppercase tracking-wide">
                        厂商参数预制
                      </h4>
                      <p className="mt-1 text-[11px] text-agent-muted-foreground">
                        按厂商文档的最优采样参数（temperature / top_p / top_k 等）自动填充请求。
                        “自动”按 Base URL + 模型名匹配内置注册表；手动 Temperature 等显式设置永远优先于预制。
                      </p>
                    </div>
                    <div className="flex gap-1.5">
                      {(
                        [
                          { value: 'auto', label: '自动（推荐）' },
                          { value: 'off', label: '关闭' },
                          { value: 'pinned', label: '指定预制' },
                        ] as { value: PresetMode; label: string }[]
                      ).map((opt) => (
                        <button
                          key={opt.value}
                          type="button"
                          data-preset-mode={opt.value}
                          onClick={() => setPresetMode(opt.value)}
                          className={`h-7 flex-1 rounded-agent-md text-xs transition-all ${
                            presetMode === opt.value
                              ? 'bg-agent-foreground text-agent-canvas'
                              : 'bg-agent-muted text-agent-muted-foreground hover:text-agent-foreground'
                          }`}
                        >
                          {opt.label}
                        </button>
                      ))}
                    </div>
                    {presetMode === 'pinned' &&
                      (presetList.length > 0 ? (
                        <select
                          value={presetPinnedIdx}
                          onChange={(e) => setPresetPinnedIdx(Number(e.target.value))}
                          className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                        >
                          {presetPinnedIdx === -1 && (
                            <option value={-1}>
                              {settings.presets?.override ? '自定义（已保存的覆盖）' : '请选择…'}
                            </option>
                          )}
                          {presetList.map((d, i) => (
                            <option key={i} value={i}>
                              {descriptorLabel(d)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <p className="text-[11px] text-agent-muted-foreground">
                          预制注册表由 sidecar 提供；sidecar 未就绪时无可选条目。
                        </p>
                      ))}
                    <p className="text-[11px] text-agent-muted-foreground/80" data-preset-preview>
                      {presetMode === 'off'
                        ? '已关闭：不下发厂商预制参数。'
                        : presetMode === 'pinned'
                          ? `生效参数：${summarizePreset(
                              presetPinnedIdx >= 0 && presetList[presetPinnedIdx]
                                ? overrideFromDescriptor(presetList[presetPinnedIdx])
                                : (settings.presets?.override ?? null),
                            )}`
                          : resolvedPreset
                            ? `命中预制：${summarizePreset(resolvedPreset)}`
                            : '当前 Base URL + 模型未命中预制，不下发额外采样参数。'}
                    </p>
                  </div>
                )}

                {settings.provider === 'openai-compat' && (
                  <div className="rounded-agent-md border border-agent-border/60 p-3 space-y-3">
                    <div>
                      <h4 className="text-xs font-semibold text-agent-muted-foreground uppercase tracking-wide">
                        高级兼容旗标（可选）
                      </h4>
                      <p className="mt-1 text-[11px] text-agent-muted-foreground">
                        仅当厂商网关与 OpenAI 协议有出入、且自动探测未覆盖时才需要改。“自动”
                        = 框架按 Base URL 主机名匹配已知厂商（DeepSeek / Moonshot / OpenRouter /
                        DashScope），未命中走 OpenAI 参考行为。
                      </p>
                    </div>
                    {compatFlags.length === 0 ? (
                      <p className="text-[11px] text-agent-muted-foreground">
                        旗标词汇表由 sidecar 提供；sidecar 未就绪时此处为空，保存不受影响。
                      </p>
                    ) : (
                      compatFlags.map((flag) => (
                        <div key={flag.key} data-compat-flag={flag.key}>
                          <label className="mb-1 block text-xs font-medium text-agent-muted-foreground">
                            {flag.key}
                          </label>
                          {flag.kind === 'bool' ? (
                            <div className="flex gap-1.5">
                              {[
                                { value: COMPAT_AUTO, label: '自动' },
                                { value: 'true', label: '开' },
                                { value: 'false', label: '关' },
                              ].map((opt) => (
                                <button
                                  key={opt.value}
                                  type="button"
                                  onClick={() =>
                                    setCompatForm((prev) => ({ ...prev, [flag.key]: opt.value }))
                                  }
                                  className={`h-7 flex-1 rounded-agent-md text-xs transition-all ${
                                    (compatForm[flag.key] ?? COMPAT_AUTO) === opt.value
                                      ? 'bg-agent-foreground text-agent-canvas'
                                      : 'bg-agent-muted text-agent-muted-foreground hover:text-agent-foreground'
                                  }`}
                                >
                                  {opt.label}
                                </button>
                              ))}
                            </div>
                          ) : flag.kind.startsWith('enum:') ? (
                            <select
                              value={compatForm[flag.key] ?? COMPAT_AUTO}
                              onChange={(e) =>
                                setCompatForm((prev) => ({ ...prev, [flag.key]: e.target.value }))
                              }
                              className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                            >
                              <option value={COMPAT_AUTO}>自动</option>
                              {flag.kind
                                .slice('enum:'.length)
                                .split(',')
                                .map((opt) => (
                                  <option key={opt} value={opt}>
                                    {opt}
                                  </option>
                                ))}
                            </select>
                          ) : (
                            <input
                              type="text"
                              value={compatForm[flag.key] === COMPAT_AUTO ? '' : (compatForm[flag.key] ?? '')}
                              onChange={(e) =>
                                setCompatForm((prev) => ({
                                  ...prev,
                                  [flag.key]: e.target.value || COMPAT_AUTO,
                                }))
                              }
                              placeholder="逗号分隔，留空 = 自动"
                              className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-2 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                            />
                          )}
                          <p className="mt-1 text-[11px] text-agent-muted-foreground/80">
                            {flag.description}
                          </p>
                        </div>
                      ))
                    )}
                  </div>
                )}

                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label className="text-xs font-medium text-agent-muted-foreground">
                      Temperature
                      {settings.temperature !== undefined && ` (${settings.temperature.toFixed(2)})`}
                    </label>
                    <div className="flex gap-1">
                      {(
                        [
                          { value: 'auto', label: '自动' },
                          { value: 'manual', label: '手动' },
                        ] as const
                      ).map((opt) => (
                        <button
                          key={opt.value}
                          type="button"
                          data-temperature-mode={opt.value}
                          onClick={() =>
                            setSettings((prev) => ({
                              ...prev,
                              temperature:
                                opt.value === 'auto'
                                  ? undefined
                                  : (prev.temperature ?? resolvedPreset?.temperature ?? 0.3),
                            }))
                          }
                          className={`h-6 rounded-agent-md px-2.5 text-[11px] transition-all ${
                            (settings.temperature === undefined ? 'auto' : 'manual') === opt.value
                              ? 'bg-agent-foreground text-agent-canvas'
                              : 'bg-agent-muted text-agent-muted-foreground hover:text-agent-foreground'
                          }`}
                        >
                          {opt.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  {settings.temperature === undefined ? (
                    <p className="text-[11px] text-agent-muted-foreground">
                      自动：命中厂商预制时用预制温度
                      {presetMode === 'auto' && resolvedPreset?.temperature != null
                        ? `（当前命中：${resolvedPreset.temperature}）`
                        : ''}
                      ，未命中则不下发（走厂商服务端默认）。手动显式值优先于预制。
                    </p>
                  ) : (
                    <input
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={settings.temperature}
                      onChange={(e) =>
                        setSettings((prev) => ({ ...prev, temperature: Number(e.target.value) }))
                      }
                      className="w-full"
                    />
                  )}
                </div>

                <div>
                  <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                    Token 预算限制 (Token Budget Limit)
                  </label>
                  <input
                    type="number"
                    value={settings.maxTotalTokens ?? 60000}
                    onChange={(e) => {
                      const val = parseInt(e.target.value, 10);
                      setSettings((prev) => ({ ...prev, maxTotalTokens: isNaN(val) ? undefined : val }));
                    }}
                    placeholder="60000"
                    className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                  />
                  <p className="mt-1.5 text-[11px] text-agent-muted-foreground">
                    单次对话中累积消耗的最大 Token 数量（默认 60,000）。超限后将自动停止，防止模型死循环或意外消耗过多 Token。
                  </p>
                </div>

                {/* ───── 超时设置 ───── */}
                <div className="border-t border-agent-border/60 pt-3 space-y-3">
                  <h4 className="text-xs font-semibold text-agent-muted-foreground uppercase tracking-wide">
                    超时设置
                  </h4>

                  <div>
                    <label className="mb-1.5 block text-xs font-medium text-agent-muted-foreground">
                      本地命令默认超时（秒）
                    </label>
                    <input
                      type="number"
                      min={1}
                      value={execTimeoutInput}
                      onChange={(e) => setExecTimeoutInput(e.target.value)}
                      placeholder="留空 = 默认（后台 30s / 终端 60s）"
                      className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
                    />
                    <p className="mt-1.5 text-[11px] text-agent-muted-foreground">
                      执行本地命令时未显式指定超时的默认等待时长。命令行带 “gui” 的启动命令不受此限制：超时后按“程序已启动、仍在运行”处理，不会被判为失败或重复启动。
                    </p>
                  </div>

                </div>
            </>
          )}

          {error && (
            <div className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
              {error}
            </div>
          )}

          {showFooterSave && (
            <div className="flex justify-end">
              <SettingsSaveButton
                testId="llm-settings-save"
                saving={saving}
                savedOk={savedOk}
                disabled={saving || loading}
                onClick={() => void handleSave()}
              />
            </div>
          )}
    </div>
  );
});
