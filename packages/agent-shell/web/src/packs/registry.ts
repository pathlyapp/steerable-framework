/**
 * 渲染层包注册表（0.3f）—— ScenarioPack.renderer 的 web 侧归宿。
 *
 * 包的渲染层代码住在 `packages/pack-<id>/web/`，经产品 web 入口
 * （products/<id>/web/main.tsx，2.3 起）静态注册——产品编译单元只含
 * 自己的包，未激活包代码物理上不进 bundle（不再依赖树摇正确性）。
 *
 * 槽位：routes / settingsPanels / chatSlots 均有真实包消费者（见各包
 * 的 web/ 目录）。
 */

import type {
  PackChatSlotAutoRevealApi,
  PackChatSlotContribution,
  PackChatSlotProps,
  PackRendererContributions,
  PackRouteContribution,
  PackSettingsPanelContribution,
} from '@steerable/pack-sdk/web';

// 类型单一真源在 @steerable/pack-sdk/web（阶段 2.2）；re-export 兼容既有调用方。
export type {
  PackChatSlotAutoRevealApi,
  PackChatSlotContribution,
  PackChatSlotProps,
  PackRendererContributions,
  PackRouteContribution,
  PackSettingsPanelContribution,
};

const routes: PackRouteContribution[] = [];
const settingsPanels: PackSettingsPanelContribution[] = [];
const chatSlots: PackChatSlotContribution[] = [];
const hiddenSlashSkills = new Set<string>();
const registeredPacks = new Set<string>();

/** 注册一个包的渲染层贡献。重复注册同一包抛错（组装期笔误，fail fast）。 */
export function registerPackRenderer(packId: string, contributions: PackRendererContributions): void {
  if (registeredPacks.has(packId)) {
    throw new Error(`[pack-renderer] duplicate registration for pack: ${packId}`);
  }
  registeredPacks.add(packId);
  routes.push(...(contributions.routes ?? []));
  settingsPanels.push(...(contributions.settingsPanels ?? []));
  chatSlots.push(...(contributions.chatSlots ?? []));
  for (const name of contributions.hiddenSlashSkills ?? []) hiddenSlashSkills.add(name);
}

export function getPackRoutes(): readonly PackRouteContribution[] {
  return routes;
}

export function getPackSettingsPanels(): readonly PackSettingsPanelContribution[] {
  return settingsPanels;
}

export function getPackChatSlots(): readonly PackChatSlotContribution[] {
  return chatSlots;
}

/** 包贡献的隐藏 slash 技能名集合（bootstrap 完成后稳定，渲染期只读）。 */
export function getPackHiddenSlashSkills(): ReadonlySet<string> {
  return hiddenSlashSkills;
}
