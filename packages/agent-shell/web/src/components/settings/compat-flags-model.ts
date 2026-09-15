import type { CompatFlagDescriptor, OpenAICompatOverrides } from '@/lib/local-api';

/**
 * compat 旗标设置区的纯状态模型——无 React、无运行时框架依赖，node 侧
 * vitest 直接可测（与 orchestration-children-model 同一拆分理由）。
 *
 * 表单值统一为字符串三态：'auto'（不覆盖，走框架 URL 自动探测）|
 * 'true'/'false'（bool 旗标）| 枚举选项 | 逗号分隔列表（string-list）。
 */

export const COMPAT_AUTO = 'auto';

export type CompatFormState = Record<string, string>;

/** 已持久化的覆盖 → 表单初值；未覆盖的旗标落在 'auto'。 */
export function formStateFromOverrides(
  overrides: OpenAICompatOverrides | undefined,
  flags: CompatFlagDescriptor[],
): CompatFormState {
  const state: CompatFormState = {};
  const source = (overrides ?? {}) as Record<string, unknown>;
  for (const flag of flags) {
    const value = source[flag.key];
    if (value === undefined || value === null) {
      state[flag.key] = COMPAT_AUTO;
    } else if (flag.kind === 'bool') {
      state[flag.key] = value === true ? 'true' : 'false';
    } else if (flag.kind === 'string-list') {
      state[flag.key] = Array.isArray(value) ? value.join(', ') : COMPAT_AUTO;
    } else {
      state[flag.key] = String(value);
    }
  }
  return state;
}

/** 表单状态 → 覆盖载荷；全部 'auto'/空时返回 undefined（= 不下发覆盖）。 */
export function overridesFromFormState(
  state: CompatFormState,
  flags: CompatFlagDescriptor[],
): OpenAICompatOverrides | undefined {
  const out: Record<string, unknown> = {};
  for (const flag of flags) {
    const raw = (state[flag.key] ?? COMPAT_AUTO).trim();
    if (raw === COMPAT_AUTO || raw === '') continue;
    if (flag.kind === 'bool') {
      if (raw === 'true' || raw === 'false') out[flag.key] = raw === 'true';
    } else if (flag.kind === 'string-list') {
      const list = raw.split(',').map((v) => v.trim()).filter(Boolean);
      if (list.length > 0) out[flag.key] = list;
    } else if (flag.kind.startsWith('enum:')) {
      const options = flag.kind.slice('enum:'.length).split(',');
      if (options.includes(raw)) out[flag.key] = raw;
    }
  }
  return Object.keys(out).length > 0 ? (out as OpenAICompatOverrides) : undefined;
}
