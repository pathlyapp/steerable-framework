/**
 * 包智能体种子注册表 —— ScenarioPack 的种子槽位（0.3d）。
 *
 * 与 pack-migrations 同一模式：`LocalStore` 单例在模块求值时构造并跑
 * `seedDefaults()`，所以种子必须在任何 `storage/index.js` import 之前
 * 由 `packs/active.ts` 注册。
 *
 * 与迁移不同的一点：**未激活的包也要注册**（active=false）——已有库
 * 里「从未定制过」的包内置智能体需要在包缺席的产品里归档，shell 只有
 * 拿到种子数据才能识别「未定制」。
 *
 * 本模块刻意不 import `storage/index.js`，避免反向依赖。判定函数是纯
 * 函数，单测直接覆盖（tests/pack-seeds.test.ts）。
 */

import type { AgentSeed } from '../scenario/pack.js';

export interface PackAgentSeedRegistration {
  packId: string;
  seeds: readonly AgentSeed[];
  /** 包在当前产品是否激活；未激活包的种子只跑「未定制则归档」。 */
  active: boolean;
}

const registrations: PackAgentSeedRegistration[] = [];

/** 注册一个包的智能体种子。重复注册同一包抛错（组装期笔误，fail fast）。 */
export function registerPackAgentSeeds(
  packId: string,
  seeds: readonly AgentSeed[],
  active: boolean,
): void {
  if (registrations.some((r) => r.packId === packId)) {
    throw new Error(`[pack-seeds] duplicate registration for pack: ${packId}`);
  }
  registrations.push({ packId, seeds, active });
}

/** 已注册的全部种子，按注册顺序返回。 */
export function getPackAgentSeeds(): readonly PackAgentSeedRegistration[] {
  return registrations;
}

/** chat_agents 行中种子判定需要的最小列。 */
export interface AgentSeedRow {
  name: string;
  description: string | null;
  role_prompt: string;
  is_archived: number;
}

/** 命中当前代文案：名字 + 描述全等、提示词全等。 */
function matchesCurrent(seed: AgentSeed, row: AgentSeedRow): boolean {
  return (
    row.name === seed.name &&
    (row.description ?? '') === (seed.description ?? '') &&
    row.role_prompt === seed.rolePrompt
  );
}

/** 命中任一历史代文案（提示词前缀匹配，描述缺省按当前代）。 */
export function matchesPreviousIdentity(seed: AgentSeed, row: AgentSeedRow): boolean {
  return (seed.previousIdentities ?? []).some(
    (prev) =>
      row.name === prev.name &&
      (row.description ?? '') === (prev.description ?? seed.description ?? '') &&
      row.role_prompt.startsWith(prev.rolePromptHead),
  );
}

/** 当前代或任一历史代全中 = 用户从未定制过这行内置智能体。 */
export function isUntouchedSeed(seed: AgentSeed, row: AgentSeedRow): boolean {
  return matchesCurrent(seed, row) || matchesPreviousIdentity(seed, row);
}
