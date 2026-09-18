/**
 * 包装配注册表（阶段 2.3）——纯构建期组合的宿主侧机制。
 *
 * 宿主（host/runtime.ts、main.ts、server/index.ts）不再静态 import 任何
 * 包代码：产品组装根（products/<id>/active.ts）把包的装配函数注册进本
 * 注册表，宿主在装配期逐个调用。产品的 tsc 编译单元只 include 自己的包，
 * 他包代码物理上不进产品 dist。
 *
 * 装配分两个时机：
 *  - import 期（active.ts 顶层）：迁移/种子/品牌等无宿主依赖的注册；
 *  - 装配期（createHostRuntime 内）：服务/工具/路由等需要宿主能力的部分，
 *    经 {@link PackAssemblyDeps} 注入。
 * 入口差异：CS（main.ts）额外消费 handle.ipc，BS（server/index.ts）额外
 * 消费 handle.httpRoutes——包的入口面贡献由宿主循环驱动，宿主代码里
 * 不出现任何包名。
 */

import type {
  HttpRouteContribution,
  IpcContribution,
  ToolContribution,
} from '../scenario/pack.js';
import type { PackDbAccess } from '../storage/driver.js';

/** 宿主注入包的装配能力面。包不 import 宿主装配模块，全部经回调注入。 */
export interface PackAssemblyDeps {
  /** Scope-bound database access; the underlying driver connection is hidden. */
  readonly packDb: PackDbAccess;
  /** 模型可见工具面（与普通回合同源）。 */
  readonly listTools: () => { name: string; description: string; inputSchema: unknown }[];
  /** chatId → 绑定项目（无项目对话返回 null）。与 router.resolveChatProject 同源。 */
  readonly resolveChatProject: (chatId: string) => Promise<{ name: string; folderPath: string } | null>;
  /** 面向全部用户面的事件广播（CS=所有窗口 IPC，BS=SSE 总线）。 */
  readonly broadcast: (channel: string, payload: unknown) => void;
  /** 工具贡献注册口（toolRouter.registerToolContributions 的收窄面）。 */
  readonly registerTools: (tools: readonly ToolContribution[]) => void;
  /** 用量归因（包的独立流不经 router 记账路径，不注入会静默漏账）。 */
  readonly recordUsage: (input: {
    chatId: string;
    kind: string;
    provider: string | null;
    model: string | null;
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    cachedPromptTokens?: number;
    costUsd?: number | null;
  }) => Promise<void>;
  /** 行为洞察（与主对话同款 turn 事件）。 */
  readonly recordInsight: (input: {
    chatId: string;
    mode?: string;
    modelId?: string | null;
    completionStatus?: string;
    durationMs?: number | null;
    toolNames?: unknown;
    userText?: string;
    assistantText?: string;
  }) => Promise<void>;
  /** 装配期日志行（进宿主日志）。 */
  readonly onLog: (line: string) => void;
}

/** CS 宿主的窗口能力：打开一个加载应用内路由的独立窗口。 */
export interface PackHostWindowCapabilities {
  /** route 如 '/<pack>-debug-log'；title 为窗口副标题（宿主拼品牌名）。 */
  openRouteWindow(route: string, options: { title: string }): Promise<void>;
}

/** 装配产物：宿主按入口面消费。全部槽位可选。 */
export interface PackAssemblyHandle {
  /** 宿主关停钩子（进程退出时按装配逆序调用）。 */
  readonly dispose?: () => void | Promise<void>;
  /** CS：包 IPC 贡献（需要宿主窗口能力时经 caps 注入）。 */
  readonly ipc?: (caps: PackHostWindowCapabilities) => readonly IpcContribution[];
  /** BS：/host/<packId>/* 路由贡献。 */
  readonly httpRoutes?: () => readonly HttpRouteContribution[];
}

export type PackAssembly = (deps: PackAssemblyDeps) => PackAssemblyHandle | void;

const assemblies = new Map<string, PackAssembly>();

/** 注册包的装配函数（产品组装根在 import 期调用；重复注册同包抛错）。 */
export function registerPackAssembly(packId: string, assembly: PackAssembly): void {
  if (assemblies.has(packId)) {
    throw new Error(`[pack-assembly] duplicate registration for pack: ${packId}`);
  }
  assemblies.set(packId, assembly);
}

/** 全部已注册装配（按注册序）。宿主装配期消费。 */
export function getPackAssemblies(): ReadonlyMap<string, PackAssembly> {
  return assemblies;
}

/** 测试钩子：清空注册表。 */
export function resetPackAssemblies(): void {
  assemblies.clear();
}
