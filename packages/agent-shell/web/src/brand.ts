/**
 * 渲染进程品牌单一真源。品牌文案在 web 构建时由产品 vite 配置注入
 * （VITE_BRAND_* define：product.json brand 优先，其次激活包 pack.json
 * brand，3.1）；logo 资产由激活包的 web 模块在注册时经
 * setBrandLogoUrl() 注入（资产静态 import 在包内，只进本产品 bundle）。
 * 未注入时一律回落到 shell 中性默认（Steerable Shell / generic logo）。
 * 主进程侧对应物见 src/brand.ts。
 */
import shellLogoUrl from '@/assets/logo-generic.png';

/** 当前构建的 flavor（开放字符串，0.3g 起不再是二值联合）。 */
export const APP_FLAVOR: string = import.meta.env.VITE_APP_FLAVOR ?? 'generic';

export const LOCAL_ASSISTANT_AGENT_ID = 'local-assistant';

export const BRAND_NAME: string = import.meta.env.VITE_BRAND_NAME ?? 'Steerable Shell';

/** shell 默认 logo（中性通用图标）；包品牌 logo 由包 web 模块注册覆盖。 */
let brandLogoUrl: string = shellLogoUrl;

/**
 * 注册产品品牌 logo（包的 register*Renderer() 在 bootstrap 前调用）。
 * 重复注册抛错（组装期笔误，fail fast——与 node 侧 setProductBrand 同语义）。
 */
export function setBrandLogoUrl(url: string): void {
  if (brandLogoUrl !== shellLogoUrl && brandLogoUrl !== url) {
    throw new Error('[brand] logo already set');
  }
  brandLogoUrl = url;
}

/** 当前产品品牌 logo（bootstrap 之后读取——包的注册已完成）。 */
export function getBrandLogoUrl(): string {
  return brandLogoUrl;
}

/** 首页 / 新对话未选手动专家时绑定的内置智能体（包品牌可覆盖）。 */
export const DEFAULT_AGENT_ID: string =
  import.meta.env.VITE_DEFAULT_AGENT_ID ?? LOCAL_ASSISTANT_AGENT_ID;

/**
 * 从当前可见专家目录里挑默认 id：优先当前构建的默认智能体（包品牌经
 * VITE_DEFAULT_AGENT_ID 注入），缺席则退回列表第一项。
 */
export function pickDefaultAgentId(agents: ReadonlyArray<{ id: string }>): string | null {
  if (agents.length === 0) return null;
  return agents.some((agent) => agent.id === DEFAULT_AGENT_ID) ? DEFAULT_AGENT_ID : agents[0].id;
}
