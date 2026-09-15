/**
 * 兼容性 re-export（阶段 2.2）：场景包契约类型的单一真源已迁入
 * `@steerable/pack-sdk`（packages/pack-sdk，零依赖纯类型包）。宿主内部
 * 既有调用方继续从本模块取类型；包代码一律直接依赖 '@steerable/pack-sdk'。
 *
 * 契约全文与设计约束见 packages/pack-sdk/types/index.d.ts 头注释。
 */
export type {
  ScenarioId,
  ToolMode,
  ToolExposure,
  PackToolContext,
  PackToolHandler,
  ToolContribution,
  ServiceFactory,
  ServiceCreateContext,
  MigrationContribution,
  SkillContribution,
  AgentSeedIdentity,
  AgentSeed,
  IpcContribution,
  HttpRouteContribution,
  HttpRouteRequest,
  MainContribution,
  PreloadContribution,
  ComponentRef,
  SettingsPanelContribution,
  RouteContribution,
  ChatSlotContribution,
  CardRendererContribution,
  RendererContribution,
  BrandSpec,
  PackagingSpec,
  ScenarioPack,
  PackBackendRouteResponse,
  PackBackendRouteRequest,
  PackBackendRoute,
  PackTurnObserver,
  PackTurnHooks,
} from '@steerable/pack-sdk';
