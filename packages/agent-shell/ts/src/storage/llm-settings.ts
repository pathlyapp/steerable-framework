/**
 * Pure LLM-settings defaults/migration logic, split out of `storage/index.ts`
 * so it can be unit-tested without touching `better-sqlite3` / `electron`
 * (importing `storage/index.ts` eagerly constructs a `LocalStore`, which
 * calls `app.getPath()` and loads the Electron-ABI-compiled native module —
 * neither works under plain Node/vitest).
 */

import { getBrand } from '../brand.js';

/**
 * OpenAI 兼容旗标的宿主覆盖（wire 形状与框架 compat.py 的
 * `OpenAICompatFlags.from_dict` 对齐——camelCase 键、未知键在框架侧
 * fail loud）。字段全部可选：缺省 = 不覆盖，由框架按 baseUrl 主机名
 * 自动探测（PROVIDER_COMPAT_HOSTS）兜底。旗标词汇表由 sidecar 的
 * `compat.describe` RPC 服务化，设置页按它渲染，不在前端硬编码键名。
 */
export interface OpenAICompatOverrides {
  supportsUsageInStreaming?: boolean;
  maxTokensField?: 'max_tokens' | 'max_completion_tokens';
  supportsReasoningEffort?: boolean;
  supportsTemperature?: boolean;
  reasoningDeltaFields?: string[];
  reasoningEchoField?: string;
  supportsForcedToolChoice?: boolean;
  /**
   * thinking 模式下带 tool_calls 的 assistant 消息即使没产出思考，也必须
   * 回传 reasoning 字段（空串）——DeepSeek 缺键即 400；框架按 baseUrl
   * 自动为 api.deepseek.com 打开，这里只是允许设置页显式覆盖。
   */
  echoEmptyReasoningForToolCalls?: boolean;
  cachedTokensFields?: string[];
}

/**
 * 厂商参数预制（框架 llm.presets）的宿主选择。缺省 / `{enabled: true}` =
 * 自动：框架按 baseUrl 主机名 + 模型名匹配注册表，填充厂商文档最优的
 * temperature/top_p/top_k 等（显式请求字段永远优先）；`enabled: false` =
 * 整层关闭；`override` = 钉死一条显式预制（设置页从 sidecar 的
 * `presets.describe` 选条目）。wire 形状与框架 `ProviderPreset.from_dict`
 * 对齐——camelCase 键、未知键在框架侧 fail loud。
 */
export interface ProviderPresetOverride {
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  reasoningEffort?: string;
  extraBody?: Record<string, unknown>;
}

export interface ProviderPresetsChoice {
  enabled?: boolean;
  override?: ProviderPresetOverride;
}

export type LlmProvider =
  | 'ollama'
  | 'openai-compat'
  | 'anthropic'
  | 'google'
  | 'openai-responses';

export interface LlmSettings {
  provider: LlmProvider;
  /**
   * 设置页选中的服务商目录 id（deepseek / anthropic / custom 等）。
   * 缺省时由 baseUrl 主机名回推。只影响表单回显，不进 sidecar wire。
   */
  vendorId?: string;
  model: string;
  baseUrl?: string;
  apiKey?: string;
  /** 缺省 = 自动：命中厂商预制时用预制值，否则不下发（厂商服务端默认）。 */
  temperature?: number;
  systemPrompt?: string;
  maxTotalTokens?: number;
  /** 见 OpenAICompatOverrides；仅 openai-compat provider 生效。 */
  compat?: OpenAICompatOverrides;
  /** 见 ProviderPresetsChoice；仅 openai-compat / openai-responses 生效。 */
  presets?: ProviderPresetsChoice;
  /**
   * 本地命令（local_exec_shell）的默认超时（秒）。不设 = 用内置默认
   * （headless 30s / 可见终端 60s）。带 "gui" 的启动命令不受它限制——
   * 超时后按"已启动、仍在运行"处理，见 local-executor.isGuiLaunchCommand。
   */
  execTimeoutSeconds?: number;
}

export const DEFAULT_SYSTEM_PROMPT =
  `你是 ${getBrand().agentName}，${getBrand().tagline}，优先给出可执行建议并在需要时调用本地工具。`;

// ─── Per-provider 预设 ──────────────────────────────────────────────────────
// 每套预设描述"用户切到该 provider 时一组合理的开箱即用值"。两套预设地位
// 对等——挑哪个当 first-install 默认由下面 DEFAULT_LLM_SETTINGS 显式声明。
//
// 前端设置页的服务商列表由 sidecar catalog.describe 提供；这里只保留
// 切 provider 时的连接缺省。

// OpenAI 兼容预设：指向 DeepSeek 官方 API。API Key 默认留空——不再打包
// 内置 key：打包进客户端 = 任何拿到 .exe 的人都能解出来，且 key 过期后
// 新用户开箱即 401（0.0.35 事故）。新用户由设置页引导填自己的 key。
export const OPENAI_COMPAT_DEFAULTS: LlmSettings = {
  provider: 'openai-compat',
  vendorId: 'deepseek',
  model: 'deepseek-chat',
  baseUrl: 'https://api.deepseek.com',
  // temperature 缺省 = 自动：命中厂商预制（llm.presets）时用厂商文档最优
  // 值，未命中则不下发。设置页可切手动。
  systemPrompt: DEFAULT_SYSTEM_PROMPT,
  maxTotalTokens: 60000,
};

// 0.0.35 及之前打包进客户端的内置 DeepSeek key（已作废，上游返回 401）。
// 保留常量仅作迁移指纹：老安装的已存设置若还带着它，升级后静默清空，
// 否则用户永远拿着一把死 key，报 401 也不知道为什么。
export const EXPIRED_BAKED_API_KEY = 'sk-b1383d225ee44f118ad99e359ec9e5d8';

/** 已存设置是否还挂着作废的出厂内置 key。 */
export function llmSettingsCarryExpiredBakedKey(s: LlmSettings): boolean {
  return !!s.apiKey && s.apiKey === EXPIRED_BAKED_API_KEY;
}

// Ollama 预设：用户切到 Ollama provider 时的默认连接参数（指向本机默认端口
// + 常用的 llama3.1:8b 镜像）。同时也充当"判断老安装是否还停留在出厂 Ollama
// 配置"的迁移指纹——见 llmSettingsMatchOllamaDefaults。
export const OLLAMA_DEFAULTS: LlmSettings = {
  provider: 'ollama',
  vendorId: 'ollama',
  model: 'llama3.1:8b',
  baseUrl: 'http://127.0.0.1:11434',
  systemPrompt: DEFAULT_SYSTEM_PROMPT,
  maxTotalTokens: 60000,
};

const BARE_PROVIDER_DEFAULTS: LlmSettings = {
  provider: 'openai-compat',
  model: '',
  systemPrompt: DEFAULT_SYSTEM_PROMPT,
  maxTotalTokens: 60000,
};

export function sanitizeLlmProvider(raw: unknown): LlmProvider {
  if (
    raw === 'ollama' ||
    raw === 'openai-compat' ||
    raw === 'anthropic' ||
    raw === 'google' ||
    raw === 'openai-responses'
  ) {
    return raw;
  }
  return 'openai-compat';
}

export function sanitizeVendorId(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const id = raw.trim();
  return id || undefined;
}

/** Sidecar `agent.chat.stream` 的 provider 字段。 */
export function sidecarWireProvider(provider: LlmProvider): string {
  switch (provider) {
    case 'ollama':
      return 'ollama';
    case 'anthropic':
      return 'anthropic';
    case 'google':
      return 'google';
    case 'openai-responses':
      return 'openai-responses';
    default:
      return 'openai_compat';
  }
}

export function usesOpenAiCompatExtras(provider: LlmProvider): boolean {
  return provider === 'openai-compat' || provider === 'openai-responses';
}

/** Ollama OpenAI 兼容面在 /v1；本机设置存的是 daemon 根路径。 */
export function listingBaseUrl(provider: LlmProvider, baseUrl: string | undefined): string | undefined {
  if (!baseUrl) return undefined;
  const trimmed = baseUrl.replace(/\/+$/, '');
  if (provider === 'ollama' && !trimmed.endsWith('/v1')) {
    return `${trimmed}/v1`;
  }
  return trimmed;
}

/** The vendor listing URL sidecar will GET (must match gateway_catalog.listing_request). */
export function vendorModelsListUrl(provider: LlmProvider, baseUrl: string): string {
  const base = listingBaseUrl(provider, baseUrl) ?? baseUrl.replace(/\/+$/, '');
  if (provider === 'anthropic') {
    return base.endsWith('/v1') ? `${base}/models` : `${base}/v1/models`;
  }
  if (provider === 'google') {
    if (base.endsWith('/models')) return base;
    if (base.includes('/v1beta')) return `${base}/models`;
    return `${base}/v1beta/models`;
  }
  return `${base}/models`;
}

// 全新安装的出厂默认。当前选 OpenAI 兼容（DeepSeek）—— Windows 用户大多没装
// 过 Ollama，开箱用云端 API 失败率最低。
export const DEFAULT_LLM_SETTINGS: LlmSettings = OPENAI_COMPAT_DEFAULTS;

// 用作迁移指纹：老安装用户停留在出厂 Ollama 默认（从未改过任何一项），新
// 版本启动时静默迁移到新默认。任意一项被改过就视为"用户已自定义"，不动。
//
// 之前漏了 maxTotalTokens：只要 provider/model/baseUrl/apiKey/temperature/
// systemPrompt 都命中出厂值，哪怕用户特意把 maxTotalTokens 调大/调小过，也
// 会被这次静默迁移覆盖回默认值——这里补齐，让指纹覆盖 LlmSettings 的全部字段。
export function llmSettingsMatchOllamaDefaults(s: LlmSettings): boolean {
  return (
    s.provider === OLLAMA_DEFAULTS.provider &&
    s.model === OLLAMA_DEFAULTS.model &&
    s.baseUrl === OLLAMA_DEFAULTS.baseUrl &&
    !s.apiKey &&
    // 0.3 是出厂默认改为"自动"之前的遗留值：当时保存过的安装会把 0.3
    // 持久化下来，指纹必须继续认它，否则老用户丢失静默迁移。
    (s.temperature === undefined || s.temperature === null || s.temperature === 0.3) &&
    (s.systemPrompt === OLLAMA_DEFAULTS.systemPrompt || !s.systemPrompt) &&
    (s.maxTotalTokens === OLLAMA_DEFAULTS.maxTotalTokens ||
      s.maxTotalTokens === undefined ||
      s.maxTotalTokens === null)
  );
}

/**
 * Merge a (possibly partial-looking, but typed as complete) incoming
 * `LlmSettings` against the preset for *its own* `provider`, not always
 * against the OpenAI-compat/DeepSeek preset.
 *
 * Previously `setLlmSettings` always merged against
 * `{...DEFAULT_LLM_SETTINGS, ...settings}` (`DEFAULT_LLM_SETTINGS` ==
 * `OPENAI_COMPAT_DEFAULTS`). Switching the provider to Ollama without
 * explicitly clearing `apiKey` left the baked-in DeepSeek key sitting in the
 * merged Ollama settings, since a key that's simply absent from `settings`
 * doesn't shadow the spread base — it silently falls through to whatever
 * `apiKey` the base object happens to carry.
 */
export function mergeLlmSettings(settings: LlmSettings): LlmSettings {
  const base =
    settings.provider === 'ollama'
      ? OLLAMA_DEFAULTS
      : settings.provider === 'openai-compat'
        ? OPENAI_COMPAT_DEFAULTS
        : { ...BARE_PROVIDER_DEFAULTS, provider: settings.provider };
  const merged = { ...base, ...settings };
  if (!settings.vendorId) merged.vendorId = base.vendorId;
  return merged;
}

const COMPAT_BOOL_KEYS = [
  'supportsUsageInStreaming',
  'supportsReasoningEffort',
  'supportsTemperature',
  'supportsForcedToolChoice',
  'echoEmptyReasoningForToolCalls',
] as const;
const COMPAT_LIST_KEYS = ['reasoningDeltaFields', 'cachedTokensFields'] as const;
const COMPAT_STRING_KEYS = ['reasoningEchoField'] as const;

/**
 * 把设置页/POST 体里的 compat 载荷收敛成合法的 `OpenAICompatOverrides`：
 * 只保留框架 `from_dict` 认识的键、按旗标种类校正类型，空数组与非法值
 * 丢弃（缺省 = 不覆盖）。框架侧对未知键 fail loud，这里先收敛一遍，
 * 让设置页的旧版本载荷不会因为框架新增旗标而报错。
 */
export function sanitizeCompatOverrides(input: unknown): OpenAICompatOverrides | undefined {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return undefined;
  const record = input as Record<string, unknown>;
  const out: OpenAICompatOverrides = {};
  for (const key of COMPAT_BOOL_KEYS) {
    if (typeof record[key] === 'boolean') out[key] = record[key] as boolean;
  }
  if (record.maxTokensField === 'max_tokens' || record.maxTokensField === 'max_completion_tokens') {
    out.maxTokensField = record.maxTokensField;
  }
  for (const key of COMPAT_LIST_KEYS) {
    const value = record[key];
    if (Array.isArray(value)) {
      const list = value.map((v) => String(v).trim()).filter(Boolean);
      if (list.length > 0) out[key] = list;
    }
  }
  for (const key of COMPAT_STRING_KEYS) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) out[key] = value.trim();
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/**
 * 把设置页/POST 体里的 presets 载荷收敛成合法的 `ProviderPresetsChoice`：
 * 与 sanitizeCompatOverrides 同一纪律——只保留框架 `ProviderPreset.from_dict`
 * 认识的键、按类型校正，非法值丢弃（缺省 = 自动匹配）。框架侧对未知键
 * fail loud，这里先收敛一遍。
 */
export function sanitizePresetsChoice(input: unknown): ProviderPresetsChoice | undefined {
  if (!input || typeof input !== 'object' || Array.isArray(input)) return undefined;
  const record = input as Record<string, unknown>;
  const out: ProviderPresetsChoice = {};
  if (typeof record.enabled === 'boolean') out.enabled = record.enabled;
  if (record.override && typeof record.override === 'object' && !Array.isArray(record.override)) {
    const raw = record.override as Record<string, unknown>;
    const override: ProviderPresetOverride = {};
    if (typeof raw.temperature === 'number') override.temperature = raw.temperature;
    if (typeof raw.topP === 'number') override.topP = raw.topP;
    if (typeof raw.maxTokens === 'number') override.maxTokens = raw.maxTokens;
    if (typeof raw.reasoningEffort === 'string' && raw.reasoningEffort) {
      override.reasoningEffort = raw.reasoningEffort;
    }
    if (raw.extraBody && typeof raw.extraBody === 'object' && !Array.isArray(raw.extraBody)) {
      override.extraBody = raw.extraBody as Record<string, unknown>;
    }
    if (Object.keys(override).length > 0) out.override = override;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}
