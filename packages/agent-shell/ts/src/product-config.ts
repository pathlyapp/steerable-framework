/**
 * 产品注入配置（3.1）：shell 不硬编码任何产品特有的端点/链接/目录名，
 * 由产品组装根（products/<id>/active.ts）在模块求值早期注入。
 *
 * 与 brand.ts 的分工：brand 是「产品身份」（显示名/自称/默认智能体），
 * 本模块是「产品资源」（遥测端点、帮助链接、数据目录）。两者都由产品
 * 组装根注入；缺省时 shell 表现为中性框架（无遥测、无帮助链接、
 * 数据目录 .agent-shell）。
 *
 * 本模块只允许依赖 node 内置模块（与 brand.ts 同约束）。
 */

export interface ProductLinks {
  /** 帮助菜单「打开发布页」URL（通常是产品的下载/更新通道页）。 */
  releasePage?: string;
  /** 帮助菜单「访问官网」URL。 */
  website?: string;
}

export interface ProductConfig {
  /**
   * 云端遥测端点（insights flush 的 API base）。空 = 不上报（中性默认）；
   * 用户设置里的 apiBase 与环境变量仍可覆盖。
   */
  insightsApiBase?: string;
  /** 帮助菜单链接；缺省的项不渲染。 */
  links?: ProductLinks;
  /**
   * BS 模式的 userData 目录名（~/ 下）。CS 模式由 Electron 按
   * productName 分目录，不经此字段。
   */
  dataDirName?: string;
  /**
   * 主 SQLite 文件名（userData 目录下）。产品必须显式声明以保住存量
   * 数据；中性 shell 缺省 'agent-shell.db'。
   */
  dbFileName?: string;
}

let productConfig: ProductConfig | null = null;

/**
 * 注入产品配置（产品组装根在第一个 import 时调用）。重复注入抛错
 * （组装期笔误，fail fast——与 setProductBrand 同语义）。
 */
export function setProductConfig(config: ProductConfig): void {
  if (productConfig) {
    throw new Error('[product-config] product config already set');
  }
  productConfig = config;
}

/** 读取已注入的产品配置；未注入返回空对象（中性框架行为）。 */
export function getProductConfig(): ProductConfig {
  return productConfig ?? {};
}
