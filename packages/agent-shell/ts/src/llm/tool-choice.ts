/**
 * OpenAI 兼容网关上的 tool_choice 能力适配。
 *
 * 多家长的 thinking / reasoner 模型会拒 `tool_choice: "required"`
 * （以及指定函数名的 object 形式），返回 400。这不是某一家的特例：
 * DeepSeek V4、OpenAI o-series、Anthropic extended thinking 的兼容网关
 * 都出现过。这里按**能力**处理，不绑厂商：
 *
 *   1. 模型名自我声明 thinking/reasoner/o-series → 出网前把 required 降成 auto
 *   2. 未知模型仍发 required；若 400 报 tool_choice 不支持，降级重试并记住
 */

import type { LlmGenerateRequest } from './types.js';

export type OpenAiToolChoice = NonNullable<LlmGenerateRequest['toolChoice']>;

/** 本进程里已经确认拒 forced tool_choice 的模型（规范化 id）。 */
const modelsRejectingForcedToolChoice = new Set<string>();

/** 去掉网关前缀（`openai/deepseek-v4-flash` → `deepseek-v4-flash`）。 */
export function canonicalizeModelId(model: string | undefined): string {
  if (!model) return '';
  const trimmed = model.trim().toLowerCase();
  const slash = trimmed.lastIndexOf('/');
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed;
}

/**
 * 模型 id 是否自我声明为 thinking / reasoner。
 * 只看能力词和 OpenAI o-series 形态，不写厂商白名单。
 * 名字里看不出来的（如 deepseek-v4-flash）走 400 降级重试。
 */
export function modelLikelyRejectsForcedToolChoice(model: string | undefined): boolean {
  const id = canonicalizeModelId(model);
  if (!id) return false;
  if (
    id.includes('reasoner') ||
    id.includes('reasoning') ||
    id.includes('thinking')
  ) {
    return true;
  }
  // o1 / o3 / o4 / o1-mini / o3-pro —— 不要误伤 gpt-4o、grok
  if (/(^|[^a-z0-9])o[1-9]([.-]|$)/.test(id)) return true;
  return false;
}

/** 该模型是否已经（启发式或上次 400）判定不支持 forced tool_choice。 */
export function rejectsForcedToolChoice(model: string | undefined): boolean {
  const id = canonicalizeModelId(model);
  if (!id) return false;
  return modelsRejectingForcedToolChoice.has(id) || modelLikelyRejectsForcedToolChoice(id);
}

export function rememberForcedToolChoiceRejected(model: string | undefined): void {
  const id = canonicalizeModelId(model);
  if (id) modelsRejectingForcedToolChoice.add(id);
}

export function resetForcedToolChoiceCompatForTests(): void {
  modelsRejectingForcedToolChoice.clear();
}

/**
 * 400 响应是否表示「当前 thinking/reasoner 模式不接受这个 tool_choice」。
 * 按错误语义匹配，不绑某一家的文案。
 */
export function isForcedToolChoiceRejected(status: number, body: string): boolean {
  if (status !== 400) return false;
  const text = body.toLowerCase();
  if (!text.includes('tool_choice') && !text.includes('tool choice')) return false;
  return (
    text.includes('thinking') ||
    text.includes('reason') ||
    text.includes('does not support') ||
    text.includes('not support') ||
    text.includes('not supported') ||
    text.includes('unsupported') ||
    text.includes('cannot be used') ||
    text.includes('incompatible')
  );
}

/**
 * 把 harness 的 tool_choice 意图映射成当前模型吃得下的值。
 * thinking/reasoner 上把 `required` 降成 `auto`；tools 仍照常下发。
 */
export function resolveOpenAiToolChoice(
  model: string | undefined,
  requested: LlmGenerateRequest['toolChoice'],
): OpenAiToolChoice {
  const choice = requested ?? 'auto';
  if (choice === 'required' && rejectsForcedToolChoice(model)) {
    return 'auto';
  }
  return choice;
}
