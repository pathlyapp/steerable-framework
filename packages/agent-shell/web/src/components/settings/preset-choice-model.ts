/**
 * 厂商参数预制（框架 llm.presets）设置区的纯表单模型，从
 * LocalLlmSettingsModal 抽出以便单测。注册表与匹配规则都在框架侧
 * （presets.describe / presets.resolve RPC），这里只做表单态与 wire
 * 载荷之间的转换，不硬编码任何厂商参数。
 */

import type {
  ProviderPresetDescriptor,
  ProviderPresetOverride,
  ProviderPresetsChoice,
} from '@/lib/local-api';

/** 三态：自动（框架按 baseUrl+model 匹配）/ 关闭 / 钉死一条注册表行。 */
export type PresetMode = 'auto' | 'off' | 'pinned';

/** 注册表行 → 钉死时随流下发的 override 载荷（null 字段不下发）。 */
export function overrideFromDescriptor(d: ProviderPresetDescriptor): ProviderPresetOverride {
  return {
    ...(d.temperature != null && { temperature: d.temperature }),
    ...(d.topP != null && { topP: d.topP }),
    ...(d.maxTokens != null && { maxTokens: d.maxTokens }),
    ...(d.reasoningEffort != null && { reasoningEffort: d.reasoningEffort }),
    ...(d.extraBody != null && { extraBody: d.extraBody }),
  };
}

/** 选择器选项标签：`host + 前缀*`，缺省键名退化为「未命名」。 */
export function descriptorLabel(d: ProviderPresetDescriptor): string {
  const parts = [d.host, d.modelPrefix ? `${d.modelPrefix}*` : null].filter(Boolean);
  return parts.join(' + ') || '（未命名预制）';
}

/** 预制参数的一行摘要，用于生效预览。 */
export function summarizePreset(p: ProviderPresetOverride | null): string {
  if (!p) return '不下发额外采样参数';
  const parts: string[] = [];
  if (p.temperature != null) parts.push(`temperature ${p.temperature}`);
  if (p.topP != null) parts.push(`top_p ${p.topP}`);
  if (p.maxTokens != null) parts.push(`max_tokens ${p.maxTokens}`);
  if (p.reasoningEffort) parts.push(`reasoning_effort ${p.reasoningEffort}`);
  if (p.extraBody) {
    for (const [k, v] of Object.entries(p.extraBody)) parts.push(`${k} ${JSON.stringify(v)}`);
  }
  return parts.length > 0 ? parts.join(' · ') : '不下发额外采样参数（该厂商最优请求即不带采样字段）';
}

export function overridesEqual(a: ProviderPresetOverride, b: ProviderPresetOverride): boolean {
  return (
    JSON.stringify({ ...a, extraBody: a.extraBody ?? null }) ===
    JSON.stringify({ ...b, extraBody: b.extraBody ?? null })
  );
}

/** 已持久化的选择 → 表单三态。 */
export function presetModeFromChoice(choice: ProviderPresetsChoice | undefined): PresetMode {
  if (!choice) return 'auto';
  if (choice.override) return 'pinned';
  return choice.enabled === false ? 'off' : 'auto';
}

/** 表单三态 → 待持久化的选择（自动 = 不持久化，框架自动匹配兜底）。 */
export function choiceFromPresetMode(
  mode: PresetMode,
  pinnedIdx: number,
  list: ProviderPresetDescriptor[],
  existing?: ProviderPresetsChoice,
): ProviderPresetsChoice | undefined {
  if (mode === 'auto') return undefined;
  if (mode === 'off') return { enabled: false };
  const pinned = pinnedIdx >= 0 ? list[pinnedIdx] : undefined;
  if (pinned) return { override: overrideFromDescriptor(pinned) };
  // 钉死但注册表不可用（sidecar 未就绪）：保留已保存的自定义覆盖。
  return existing?.override ? { override: existing.override } : undefined;
}
