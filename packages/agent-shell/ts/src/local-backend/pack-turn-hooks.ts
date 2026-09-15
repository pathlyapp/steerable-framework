/**
 * 场景包的「回合级」钩子注册表（1.2 新增）。
 *
 * 包的路由/服务可以完全挂在注册表上，但有一类扩展发生在**每一条正在
 * 流式的 CoreLoop 回合内部**。现存四个钩子位（以「文档预览包」为典型
 * 用例）：
 *
 *  1. `execWritableRoots`：包要把产物写进每会话的工作区，该目录在项目
 *     根之外，必须随回合的 exec 沙箱放行（写，不是只读根）；
 *  2. `worldState`：把工作区路径喂给模型的 world-state，catalog 路径下
 *     模型据此找到工作区；
 *  3. `forcedSkillVars`：显式召唤包技能时把技能正文里的占位符渲染出
 *     工作区路径；
 *  4. `beginTurn` 返回的回合观察器：识别「本轮真的调用了包技能」
 *     （onToolStart），在工具落定后扫描工作区并向渲染层广播产物更新
 *     （onToolSettled）——替代前端轮询。
 *
 * 钩子按包注册、按回合拼装；未注册任何钩子的产品走零开销路径
 * （空数组/undefined 合并，行为与注册前逐字节一致）。
 */

import type { PackTurnHooks, PackTurnObserver } from '@steerable/pack-sdk';

// 类型单一真源在 @steerable/pack-sdk（阶段 2.2）；re-export 兼容既有调用方。
export type { PackTurnHooks, PackTurnObserver };

const hooksByPack = new Map<string, PackTurnHooks>();

/** 注册包的回合钩子（重复注册同包后者覆盖——测试隔离靠 reset）。 */
export function registerPackTurnHooks(packId: string, hooks: PackTurnHooks): void {
  hooksByPack.set(packId, hooks);
}

/** 清空全部注册（测试用）。 */
export function resetPackTurnHooks(): void {
  hooksByPack.clear();
}

/** 全部激活包的 exec 可写根（按注册序拼接）。 */
export function collectPackExecWritableRoots(chatId: string): string[] {
  const roots: string[] = [];
  for (const hooks of hooksByPack.values()) {
    roots.push(...(hooks.execWritableRoots?.(chatId) ?? []));
  }
  return roots;
}

/** 全部激活包的 world-state 附加字段（后者覆盖同名键——包前缀命名避免碰撞）。 */
export function collectPackWorldState(chatId: string): Record<string, unknown> {
  let merged: Record<string, unknown> = {};
  for (const hooks of hooksByPack.values()) {
    const extra = hooks.worldState?.(chatId);
    if (extra) merged = { ...merged, ...extra };
  }
  return merged;
}

/** 全部激活包的强制技能变量（后者覆盖同名键）。 */
export function collectPackForcedSkillVars(chatId: string): Record<string, string> {
  let merged: Record<string, string> = {};
  for (const hooks of hooksByPack.values()) {
    const extra = hooks.forcedSkillVars?.(chatId);
    if (extra) merged = { ...merged, ...extra };
  }
  return merged;
}

/** 回合开始：收集所有包的观察器（无注册时返回空数组，调用方零分支）。 */
export function beginPackTurnObservers(ctx: {
  chatId: string;
  broadcast: ((channel: string, payload: unknown) => void) | undefined;
}): PackTurnObserver[] {
  const observers: PackTurnObserver[] = [];
  for (const hooks of hooksByPack.values()) {
    const observer = hooks.beginTurn?.(ctx);
    if (observer) observers.push(observer);
  }
  return observers;
}
