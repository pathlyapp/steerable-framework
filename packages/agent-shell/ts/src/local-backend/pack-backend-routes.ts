/**
 * 场景包 local-backend 路由注册表（1.2）。
 *
 * 与 `host/http-routes.ts`（BS 宿主 `/host/<packId>/*`）不同：本注册表管
 * `/api/v2/*` 命名空间下的包路由——CS/BS 双宿主都经 LocalBackendRouter
 * 服务这一命名空间（CS 走 localBackend.request IPC 桥，BS 走 HTTP），所以
 * 包的业务路由必须挂在这里才能双宿主可达。
 *
 * 命名空间纪律：每次注册调用声明一个路径前缀（如 '/api/v2/<pack>-<feature>/'），
 * 该批路由必须全部落在前缀下；匹配发生在宿主自带路由之后、fallback 之前，
 * 包不能遮蔽宿主路由。
 */

import type {
  PackBackendRoute,
  PackBackendRouteRequest,
  PackBackendRouteResponse,
} from '@steerable/pack-sdk';

// 类型单一真源在 @steerable/pack-sdk（阶段 2.2）；re-export 兼容既有调用方。
export type { PackBackendRoute, PackBackendRouteRequest, PackBackendRouteResponse };

interface RegisteredPackBackendRoute extends PackBackendRoute {
  readonly packId: string;
}

const packBackendRoutes: RegisteredPackBackendRoute[] = [];

/**
 * 注册一批包路由；`pathPrefix` 是本批路由的命名空间前缀（必须以
 * '/api/v2/' 开头），越出前缀的路由在组装期直接抛错。
 */
export function registerPackBackendRoutes(
  packId: string,
  pathPrefix: string,
  routes: readonly PackBackendRoute[],
): void {
  if (!pathPrefix.startsWith('/api/v2/')) {
    throw new Error(
      `[packs] local-backend 路由前缀 "${pathPrefix}" 不合规：包 "${packId}" 的前缀必须在 /api/v2/ 命名空间下`,
    );
  }
  for (const route of routes) {
    if (!route.path.startsWith(pathPrefix)) {
      throw new Error(
        `[packs] local-backend 路由 "${route.path}" 不合规：包 "${packId}" 该批路由必须在 ${pathPrefix} 前缀下`,
      );
    }
    packBackendRoutes.push({ ...route, packId });
  }
}

/** 测试钩子：清空注册表。 */
export function resetPackBackendRoutes(): void {
  packBackendRoutes.length = 0;
}

/**
 * 匹配已注册的包路由。段参数（`:id`）逐段捕获进 params；无匹配返回 null。
 */
export function matchPackBackendRoute(
  method: string,
  pathname: string,
): { route: RegisteredPackBackendRoute; params: Record<string, string> } | null {
  for (const route of packBackendRoutes) {
    if (route.method !== method) continue;
    const params = matchPath(route.path, pathname);
    if (params) return { route, params };
  }
  return null;
}

function matchPath(pattern: string, pathname: string): Record<string, string> | null {
  const patternSegs = pattern.split('/');
  const pathSegs = pathname.split('/');
  if (patternSegs.length !== pathSegs.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < patternSegs.length; i += 1) {
    const seg = patternSegs[i];
    if (seg.startsWith(':')) {
      params[seg.slice(1)] = decodeURIComponent(pathSegs[i]);
    } else if (seg !== pathSegs[i]) {
      return null;
    }
  }
  return params;
}
