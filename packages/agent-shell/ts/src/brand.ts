/**
 * 品牌/ flavor 单一真源（主进程与 Node 侧共用）。
 *
 * flavor 是开放字符串（ScenarioId，0.3g 起不再是二值联合）：`generic`
 * 是无场景包的产品 flavor，其余 flavor 由场景包激活。合法 flavor 集合
 * = 应用层 products/manifest.json 的 flavors 键（best-effort 校验，
 * 读不到清单时不强制）。
 *
 * flavor 在打包时由 electron-builder.js 通过 extraMetadata.flavor
 * 写进包内 package.json；运行时从这里读回。dev 优先读 APP_FLAVOR，
 * 再回退包内 package.json 的 flavor 字段，最终兜底 generic。
 *
 * 品牌数据（3.1 起全部产品注入）：产品组装根（products/<id>/active.ts）
 * 在模块求值早期调用 setProductBrand()——数据源是 product.json 的
 * brand 字段（无包产品）或激活包的 pack.json brand（0.3g 机制）。
 * shell 默认品牌是中性占位（Steerable Shell），不注入不出产品名。
 * getBrand() 在覆盖注入后重算。
 *
 * 本模块只允许依赖 node 内置模块——llm-settings.ts 会被 vitest 直接 import，
 * 不能碰 electron / better-sqlite3。
 */
import fs from 'node:fs';
import path from 'node:path';

import { getAppRootDir } from './runtime.js';
import type { ScenarioId } from './scenario/pack.js';

/** 开放字符串（0.3g）：'generic' | 各场景产品 flavor | 未来产品 id。 */
export type AppFlavor = ScenarioId;

/** shell 默认智能体；场景产品里仍可选，但不一定作为首页默认。 */
export const LOCAL_ASSISTANT_AGENT_ID = 'local-assistant';
/** 内置「智能助手」：种子时 `loadAllSkills` 开启（无视触发条件全量加载技能）。 */
export const ALL_ROUND_ASSISTANT_AGENT_ID = 'all-round-assistant';

export interface Brand {
  flavor: AppFlavor;
  /** UI 显示名：窗口标题、通知标题等 */
  displayName: string;
  /** LLM 身份自称（系统提示词里用） */
  agentName: string;
  /** 一句话定位（fallback prompt 用） */
  tagline: string;
  /** 新对话 / 首页未选手动专家时绑定的内置智能体 id */
  defaultAgentId: string;
}

/**
 * shell 默认品牌——中性框架占位（3.1 起产品名一律由产品组装根注入，
 * 见 products/<id>/product.json 的 brand 字段或激活包的 pack.json）。
 */
const SHELL_BRAND: Omit<Brand, 'flavor'> = {
  displayName: 'Steerable Shell',
  agentName: 'Agent',
  tagline: '一款本地桌面 AI 伙伴',
  defaultAgentId: LOCAL_ASSISTANT_AGENT_ID,
};

let productBrand: Omit<Brand, 'flavor'> | null = null;
let cached: Brand | null = null;

/**
 * 注入产品品牌（产品组装根在第一个 import 时调用）。重复注入抛错
 * （组装期笔误，fail fast）。注入后作废缓存，后续 getBrand() 重算。
 */
export function setProductBrand(brand: Omit<Brand, 'flavor'>): void {
  if (productBrand) {
    throw new Error('[brand] product brand already set');
  }
  productBrand = brand;
  cached = null;
}

/** 合法 flavor 集合（应用层 products/manifest.json 的 flavors 键）；读不到返回 null（不强制）。 */
function knownFlavors(): string[] | null {
  try {
    // 3.2 起 flavor→包 清单是应用层数据（products/manifest.json），由产品
    // 组装根经 setAppRootDir 注入锚点；shell 包内不再携带该清单。
    const manifest = JSON.parse(
      fs.readFileSync(path.join(getAppRootDir(), 'products', 'manifest.json'), 'utf8'),
    ) as { flavors?: Record<string, unknown> };
    return Object.keys(manifest.flavors ?? {});
  } catch {
    return null;
  }
}

function detectFlavor(): AppFlavor {
  const known = knownFlavors();
  const isKnown = (value: string): boolean => !known || known.includes(value);
  const fromEnv = process.env.APP_FLAVOR;
  if (fromEnv) {
    if (isKnown(fromEnv)) return fromEnv;
    throw new Error(
      `Unknown APP_FLAVOR=${fromEnv}. Known flavors: ${known?.join(', ') ?? '(manifest unavailable)'}`,
    );
  }
  try {
    // 应用根 package.json 的 flavor 字段（打包时 electron-builder 经
    // extraMetadata.flavor 写入包内 package.json；3.2 起应用根由产品注入）。
    const pkgPath = path.join(getAppRootDir(), 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8')) as {
      flavor?: string;
    };
    if (pkg.flavor && isKnown(pkg.flavor)) return pkg.flavor;
  } catch {
    // 读不到 package.json 时用兜底
  }
  return 'generic';
}

export function getBrand(): Brand {
  if (!cached) {
    const flavor = detectFlavor();
    cached = { flavor, ...(productBrand ?? SHELL_BRAND) };
  }
  return cached;
}
