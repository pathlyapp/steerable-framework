/**
 * 设置页服务商选择：框架 sidecar `catalog.describe` 是目录真源，这里只做
 * 常用置顶、中文标签、以及 sidecar 未就绪时的本地回退。
 */

import type {
  CatalogProviderDescriptor,
  GatewayModelEntry,
  LlmProvider,
  LlmSettings,
} from '@/lib/local-api';
import { unknownGatewayEntry } from '@/components/settings/model-capabilities';

export interface VendorOption {
  id: string;
  label: string;
  apiBaseUrl: string | null;
  wireKind: string;
  models: string[];
  featured: boolean;
  /** 本地注入（Ollama / 自定义），不在框架目录里。 */
  local?: boolean;
}

export const FEATURED_VENDOR_IDS = [
  'deepseek',
  'alibaba-cn',
  'moonshotai-cn',
  'zhipuai',
  'openai',
  'anthropic',
  'google',
  'openrouter',
  'xai',
  'groq',
  'lmstudio',
] as const;

export const VENDOR_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google Gemini',
  openrouter: 'OpenRouter',
  'alibaba-cn': '阿里云百炼',
  'moonshotai-cn': '月之暗面 Kimi',
  zhipuai: '智谱 GLM',
  groq: 'Groq',
  xai: 'xAI',
  lmstudio: 'LM Studio',
  ollama: 'Ollama (本地)',
  custom: '自定义 OpenAI 兼容',
};

/** sidecar 未就绪时仍能选常用服务商。 */
export const FALLBACK_VENDORS: CatalogProviderDescriptor[] = [
  {
    id: 'deepseek',
    apiBaseUrl: 'https://api.deepseek.com',
    envVars: ['DEEPSEEK_API_KEY'],
    wireKind: 'openai_compat',
    models: ['deepseek-chat', 'deepseek-v4-flash', 'deepseek-v4-pro'],
  },
  {
    id: 'openai',
    apiBaseUrl: 'https://api.openai.com/v1',
    envVars: ['OPENAI_API_KEY'],
    wireKind: 'openai_compat',
    models: ['gpt-5.4', 'gpt-5.4-mini', 'gpt-4.1', 'gpt-4o'],
  },
  {
    id: 'anthropic',
    apiBaseUrl: 'https://api.anthropic.com',
    envVars: ['ANTHROPIC_API_KEY'],
    wireKind: 'anthropic',
    models: ['claude-sonnet-4-6', 'claude-opus-4-7', 'claude-haiku-4-5'],
  },
  {
    id: 'google',
    apiBaseUrl: 'https://generativelanguage.googleapis.com',
    envVars: ['GEMINI_API_KEY'],
    wireKind: 'google',
    models: ['gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-2.5-flash'],
  },
  {
    id: 'openrouter',
    apiBaseUrl: 'https://openrouter.ai/api/v1',
    envVars: ['OPENROUTER_API_KEY'],
    wireKind: 'openai_compat',
    models: [],
  },
  {
    id: 'alibaba-cn',
    apiBaseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    envVars: ['DASHSCOPE_API_KEY'],
    wireKind: 'openai_compat',
    models: ['qwen3.5-plus', 'qwen3-max', 'qwen-plus'],
  },
  {
    id: 'moonshotai-cn',
    apiBaseUrl: 'https://api.moonshot.cn/v1',
    envVars: ['MOONSHOT_API_KEY'],
    wireKind: 'openai_compat',
    models: ['kimi-k2.5', 'kimi-k2.6'],
  },
  {
    id: 'zhipuai',
    apiBaseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    envVars: ['ZHIPU_API_KEY'],
    wireKind: 'openai_compat',
    models: ['glm-5', 'glm-5.3-flash'],
  },
  {
    id: 'groq',
    apiBaseUrl: 'https://api.groq.com/openai/v1',
    envVars: ['GROQ_API_KEY'],
    wireKind: 'openai_compat',
    models: [],
  },
  {
    id: 'xai',
    apiBaseUrl: 'https://api.x.ai/v1',
    envVars: ['XAI_API_KEY'],
    wireKind: 'openai-responses',
    models: ['grok-4', 'grok-3'],
  },
  {
    id: 'lmstudio',
    apiBaseUrl: 'http://127.0.0.1:1234/v1',
    envVars: ['LMSTUDIO_API_KEY'],
    wireKind: 'openai_compat',
    models: [],
  },
];

const LOCAL_VENDORS: VendorOption[] = [
  {
    id: 'ollama',
    label: VENDOR_LABELS.ollama,
    apiBaseUrl: 'http://127.0.0.1:11434',
    wireKind: 'ollama',
    models: ['llama3.1:8b', 'qwen2.5:14b', 'qwen2.5-coder:32b'],
    featured: true,
    local: true,
  },
  {
    id: 'custom',
    label: VENDOR_LABELS.custom,
    apiBaseUrl: null,
    wireKind: 'openai_compat',
    models: [],
    featured: true,
    local: true,
  },
];

export function vendorLabel(id: string): string {
  return VENDOR_LABELS[id] ?? id;
}

export function llmProviderFromWireKind(wireKind: string): LlmProvider {
  if (wireKind === 'ollama') return 'ollama';
  if (wireKind === 'anthropic') return 'anthropic';
  if (wireKind === 'google') return 'google';
  if (wireKind === 'openai-responses') return 'openai-responses';
  return 'openai-compat';
}

export function usesOpenAiCompatExtras(provider: LlmProvider): boolean {
  return provider === 'openai-compat' || provider === 'openai-responses';
}

function hostOf(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url.includes('://') ? url : `https://${url}`).hostname.toLowerCase();
  } catch {
    return null;
  }
}

export function mergeVendorOptions(catalog: CatalogProviderDescriptor[]): VendorOption[] {
  const featured = new Set<string>(FEATURED_VENDOR_IDS);
  const fromCatalog: VendorOption[] = catalog.map((row) => ({
    id: row.id,
    label: vendorLabel(row.id),
    apiBaseUrl: row.apiBaseUrl,
    wireKind: row.wireKind,
    models: row.models,
    featured: featured.has(row.id),
  }));
  const seen = new Set(fromCatalog.map((v) => v.id));
  const extras = LOCAL_VENDORS.filter((v) => !seen.has(v.id));
  const featuredOrder = [...FEATURED_VENDOR_IDS, 'ollama', 'custom'];
  const rank = (id: string) => {
    const idx = featuredOrder.indexOf(id);
    return idx === -1 ? featuredOrder.length : idx;
  };
  return [...fromCatalog, ...extras].sort((a, b) => {
    const byFeatured = Number(b.featured) - Number(a.featured);
    if (byFeatured !== 0) return byFeatured;
    const byRank = rank(a.id) - rank(b.id);
    if (byRank !== 0) return byRank;
    return a.label.localeCompare(b.label, 'zh');
  });
}

export function inferVendorId(settings: LlmSettings, vendors: VendorOption[]): string {
  if (settings.vendorId && vendors.some((v) => v.id === settings.vendorId)) {
    return settings.vendorId;
  }
  if (settings.provider === 'ollama') return 'ollama';
  const host = hostOf(settings.baseUrl);
  if (host) {
    const hit = vendors.find((v) => hostOf(v.apiBaseUrl) === host);
    if (hit) return hit.id;
  }
  if (settings.provider === 'anthropic') return 'anthropic';
  if (settings.provider === 'google') return 'google';
  if (settings.provider === 'openai-responses') return 'xai';
  return 'custom';
}

export function defaultModelForVendor(vendor: VendorOption, current?: string): string {
  if (current && vendor.models.includes(current)) return current;
  return vendor.models[0] ?? current ?? '';
}

export type LiveCatalogStatus = 'idle' | 'live' | 'stale' | 'offline';

/**
 * 下拉优先用网关 `GET /models` 的实时目录；拉不到才回退服务商内置列表。
 * 当前已选 id 始终保留，避免保存过的模型从列表里消失。
 */
export function modelPickerOptions(
  liveModels: string[],
  catalogModels: string[],
  current: string | undefined,
  status: LiveCatalogStatus,
): string[] {
  const useLive = (status === 'live' || status === 'stale') && liveModels.length > 0;
  const primary = useLive ? liveModels : catalogModels;
  return Array.from(new Set([...primary, current ?? ''].filter((id) => id.trim())));
}

export interface ModelPickerRow {
  id: string;
  entry: GatewayModelEntry | null;
}

/**
 * 下拉行：实时目录带上 join 后的能力；当前 id 不在网关列表里时标成未识别。
 * 回退服务商内置列表时没有核实过的能力，筹码留空。
 */
export function modelPickerRows(
  liveEntries: GatewayModelEntry[],
  catalogModels: string[],
  current: string | undefined,
  status: LiveCatalogStatus,
): ModelPickerRow[] {
  const useLive = (status === 'live' || status === 'stale') && liveEntries.length > 0;
  const ids = modelPickerOptions(
    liveEntries.map((entry) => entry.id),
    catalogModels,
    current,
    status,
  );
  const byId = new Map(liveEntries.map((entry) => [entry.id, entry]));
  return ids.map((id) => {
    const entry = byId.get(id) ?? null;
    if (entry) return { id, entry };
    if (useLive) return { id, entry: unknownGatewayEntry(id) };
    return { id, entry: null };
  });
}
