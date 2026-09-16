/**
 * @steerable/pack-sdk —— 场景智能体包契约（纯类型，零运行时）。
 *
 * 一个「场景包」把一个垂直场景（如某行业的专业工具链……）需要的全部
 * 扩展声明集中在一处：主进程服务/工具/表/技能/智能体种子/IPC/HTTP 路由、
 * preload 桥、渲染层插槽、品牌与打包覆盖。宿主（CS Electron 主进程与
 * BS headless server）按产品组装时选中的包集合，逐个槽位消费这些声明。
 *
 * 设计约束：
 *  - 本包是零依赖纯类型：不允许 import 宿主内部模块（tool-router、
 *    storage 等），否则宿主消费接口时会形成循环依赖。宿主侧类型
 *    （ToolMode 等）的单一真源在本包，宿主模块 re-export 兼容。
 *  - 槽位只开有真实场景代码论证的（既有场景包 diff 的并集），
 *    不凭空膨胀。新槽位 = 新场景的真实需求驱动。
 *  - 组合发生在构建期：产品的 composition root 静态 import 自己的包；
 *    渲染层受 Electron CSP + preload 白名单约束，不做运行时动态加载。
 *
 * 渲染层贡献类型在 './web' 子路径（依赖 React 类型，主进程侧勿引）。
 *
 * 槽位集合由真实场景包（应用仓库内）的 diff 并集论证。
 */

/** 场景 id。开放字符串（不再是 'generic' | 'ciflog' 二值联合）。 */
export type ScenarioId = string;

/**
 * 工具安全分级与曝光层。这两个枚举的单一真源在本包（0.3a 从
 * tool-router.ts 迁入）；tool-router re-export 兼容既有调用方。
 */
export type ToolMode =
  | 'read'
  | 'ui'
  | 'synthetic'
  | 'safe_write'
  | 'destructive'
  | 'local'
  | 'external'
  | 'auto_local';

export type ToolExposure = 'direct' | 'deferred' | 'hidden';

/**
 * 工具执行上下文（与宿主 ToolExecContext 结构对齐的最小声明）。
 * 宿主在分发时注入；包不应假设字段以外的任何宿主状态。
 */
export interface PackToolContext {
  /** 项目模式沙箱根；非空时文件/shell 工具必须落在根内。 */
  projectRoot?: string | null;
  /** 调用发生的 chat id；任务类工具按它归组。 */
  chatId?: string;
}

/** 工具 handler：接收模型 JSON 参数与调用上下文，返回可 JSON 序列化的结果。 */
export type PackToolHandler = (
  args: Record<string, unknown>,
  context: PackToolContext,
) => Promise<unknown>;

/** 一个模型可见工具的贡献：schema + 执行体。 */
export interface ToolContribution {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: Record<string, unknown>;
  readonly mode: ToolMode;
  /** 缺省 'direct'。 */
  readonly exposure?: ToolExposure;
  readonly handler: PackToolHandler;
}

/**
 * 主进程长驻领域服务（如 CflogService / PptEditService）。
 * 宿主在装配期调用 create，把返回值交给包内工具/IPC/路由使用；
 * 宿主不感知服务类型——包内部通过闭包共享。
 */
export interface ServiceFactory {
  readonly serviceId: string;
  /** 装配上下文目前只承诺 broadcast；需要更多宿主能力时在这里加字段。 */
  readonly create: (ctx: ServiceCreateContext) => unknown;
  /** 可选关停钩子；宿主退出时按注册逆序调用。 */
  readonly dispose?: (service: unknown) => void | Promise<void>;
}

export interface ServiceCreateContext {
  /** 向用户面广播事件（CS=IPC webContents.send，BS=SSE 总线）。 */
  broadcast: (channel: string, payload: unknown) => void;
}

/**
 * SQLite 迁移：一串 DDL 语句（CREATE TABLE/INDEX IF NOT EXISTS …），
 * 宿主在建库迁移阶段按包声明顺序执行。表名必须带包前缀（<pack>_*
 * ppt_*），跨包不许共享表。
 */
export interface MigrationContribution {
  /** DDL 语句（CREATE TABLE/INDEX IF NOT EXISTS …），合并为一次 exec 执行。 */
  readonly ddl?: readonly string[];
  /** 增量列迁移（已有库的 ALTER TABLE ADD COLUMN，列已存在则跳过）。 */
  readonly ensureColumns?: readonly { table: string; column: string; type: string }[];
}

/** 技能贡献：包内一个含 SKILL.md 的目录，构建期拷入产物技能根。 */
export interface SkillContribution {
  /** 相对包根的目录路径（目录名即技能 id，如 '90-<pack>'）。 */
  readonly dir: string;
}

/**
 * 内置智能体种子的历史身份：名字 + 提示词开头（+ 可选描述），用于识别
 * 「从未被用户改过」的内置行——改过的行永远保留用户定制。
 */
export interface AgentSeedIdentity {
  readonly name: string;
  /** 缺省表示「描述与当前代一致」才算未定制。 */
  readonly description?: string;
  /** 历史代 rolePrompt 只做前缀匹配（旧文案可能被后续版本续写）。 */
  readonly rolePromptHead: string;
}

/**
 * 内置智能体种子。宿主在包在场时种子/按 previousIdentities 静默升级、
 * 包缺席且用户从未定制过时归档；改过的行永远保留用户定制。
 */
export interface AgentSeed {
  readonly id: string;
  readonly slug: string;
  readonly name: string;
  readonly icon?: string;
  readonly color?: string;
  readonly description?: string;
  readonly rolePrompt: string;
  readonly forbiddenPrompt?: string;
  readonly skillIds?: readonly string[];
  readonly loadAllSkills?: boolean;
  /**
   * 列表位置。0 = 主打智能体：宿主默认智能体在出厂状态下让位到 1
   * （双方都未脱离出厂位置才交换，用户调过序不动）。
   */
  readonly sortOrder?: number;
  readonly previousIdentities?: readonly AgentSeedIdentity[];
}

/** IPC 贡献：channel 必须以前缀 '<packId>:' 开头（注册表强制）。 */
export interface IpcContribution {
  readonly channel: string;
  /** 参数为 renderer 经 preload 透传的序列化载荷。 */
  readonly handler: (payload: unknown) => Promise<unknown> | unknown;
}

/** BS 模式 HTTP 路由贡献（/host/<packId>/* 命名空间，见 host/http-routes.ts）。 */
export interface HttpRouteContribution {
  readonly method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  /** 路径模式，如 '/api/v2/ppt-edits/:id'。必须带包前缀路径段。 */
  readonly path: string;
  readonly handler: (req: HttpRouteRequest) => Promise<unknown>;
}

export interface HttpRouteRequest {
  readonly params: Record<string, string>;
  readonly query: Record<string, string>;
  readonly body: unknown;
}

/** 主进程面贡献集合。 */
export interface MainContribution {
  readonly services?: readonly ServiceFactory[];
  readonly tools?: readonly ToolContribution[];
  readonly migrations?: readonly MigrationContribution[];
  readonly skills?: readonly SkillContribution[];
  readonly agentSeeds?: readonly AgentSeed[];
  readonly ipc?: readonly IpcContribution[];
  readonly httpRoutes?: readonly HttpRouteContribution[];
}

/**
 * preload 桥白名单：包在 window.electron 下挂 '<packId>' 命名空间。
 * 具体桥接函数由包的 preload 模块实现；这里只声明命名空间占用，
 * 宿主据此做 CSP/白名单装配。
 */
export interface PreloadContribution {
  readonly namespace: string;
}

/**
 * 渲染层组件引用。主进程侧不依赖 React，这里用结构化最小类型；
 * 渲染层注册表（apps/web）消费时收窄到具体组件类型。
 */
export type ComponentRef = unknown;

export interface SettingsPanelContribution {
  readonly panelId: string;
  readonly title: string;
  readonly component: ComponentRef;
}

export interface RouteContribution {
  /** hash 路由路径，如 '/<pack>-debug-log'。 */
  readonly path: string;
  readonly component: ComponentRef;
  /** 独立窗口（无侧栏）还是嵌进 AgentLayout。 */
  readonly standalone?: boolean;
}

export interface ChatSlotContribution {
  readonly slotId: string;
  readonly component: ComponentRef;
}

/** 消息卡片渲染器：按工具名/块类型匹配。 */
export interface CardRendererContribution {
  readonly match: string;
  readonly component: ComponentRef;
}

/** 渲染层面贡献集合。 */
export interface RendererContribution {
  readonly settingsPanels?: readonly SettingsPanelContribution[];
  readonly routes?: readonly RouteContribution[];
  readonly chatSlots?: readonly ChatSlotContribution[];
  readonly cardRenderers?: readonly CardRendererContribution[];
}

/** 品牌规格：运行时文案与默认智能体。 */
export interface BrandSpec {
  /** UI 显示名：窗口标题、通知标题等。 */
  readonly displayName: string;
  /** LLM 身份自称（系统提示词里用）。 */
  readonly agentName: string;
  /** 一句话定位（fallback prompt 用）。 */
  readonly tagline: string;
  /** 新对话未显式选智能体时绑定的内置智能体 id。 */
  readonly defaultAgentId: string;
}

/** 打包覆盖：产品组装期消费，不进运行时。 */
export interface PackagingSpec {
  readonly appId: string;
  readonly productName: string;
  /** 发布产物目录名（release-<x>）与下载通道段。 */
  readonly artifactDirName: string;
  readonly downloadChannel: string;
  /** 包需要额外打进产物的资源（相对包根）。 */
  readonly extraResources?: readonly string[];
}

/**
 * 场景包契约。所有槽位可选；一个包至少要有 brand（纯品牌产品）
 * 或一个实质贡献槽。
 */
export interface ScenarioPack {
  readonly id: ScenarioId;
  readonly brand: BrandSpec;
  readonly main?: MainContribution;
  readonly preload?: PreloadContribution;
  readonly renderer?: RendererContribution;
  readonly packaging?: PackagingSpec;
}

// ─── local-backend 包路由（/api/v2/<包前缀>/*）契约 ───
// 注册表实现住在宿主 src/local-backend/pack-backend-routes.ts；这里只放类型。

/** 包路由响应：与宿主 router 的 LocalBackendResponse 结构对齐。 */
export interface PackBackendRouteResponse<T = unknown> {
  status: number;
  data: T;
}

/** 包路由请求：段参数 + query + 反序列化后的 body。 */
export interface PackBackendRouteRequest {
  readonly params: Record<string, string>;
  /** URL query（searchParams 原样透传，多值键取逗号拼接语义同 URL 规范）。 */
  readonly query: URLSearchParams;
  readonly body: unknown;
}

export interface PackBackendRoute {
  readonly method: 'GET' | 'POST' | 'PUT' | 'DELETE';
  /** 路径模式，如 '/api/v2/ppt-preview/edits/:id/cancel'。 */
  readonly path: string;
  readonly handler: (req: PackBackendRouteRequest) => Promise<PackBackendRouteResponse>;
}

// ─── 回合级钩子契约 ───
// 注册表实现住在宿主 src/local-backend/pack-turn-hooks.ts；这里只放类型。

/** 一条正在流式的回合的包侧观察器（beginTurn 的返回值）。 */
export interface PackTurnObserver {
  /** 模型发起一次工具调用（尚未执行）。 */
  onToolStart?(call: { tool: string; arguments: unknown }): void;
  /** 一次工具调用执行完毕（结果已进 executedActions）。 */
  onToolSettled?(): void | Promise<void>;
}

export interface PackTurnHooks {
  /** 回合 exec 沙箱的额外可写根（按会话解析）。 */
  execWritableRoots?(chatId: string): string[];
  /** 并入 buildWorldState 的附加字段（如 ppt_workspace）。 */
  worldState?(chatId: string): Record<string, unknown>;
  /** 强制技能正文（"/技能名" 一次性注入）的额外占位符变量。 */
  forcedSkillVars?(chatId: string): Record<string, string>;
  /**
   * 回合开始时调用；返回 null 表示本回合不观察。broadcast 是宿主的用户面
   * 事件出口（CS=窗口广播，BS=SSE），包观察器经它推自己的更新事件。
   */
  beginTurn?(ctx: {
    chatId: string;
    broadcast: ((channel: string, payload: unknown) => void) | undefined;
  }): PackTurnObserver | null;
}
