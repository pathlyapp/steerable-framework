/**
 * 场景包 HTTP 路由注册表（BS 宿主扩展点，0.4）。
 *
 * 包把 `/host/<packId>/*` 前缀下的路由声明为 HttpRouteContribution，由
 * server/index.ts 在组装期注册；http-server 在宿主自带路由之后、404 之前
 * 按注册顺序精确/段参数匹配。路径必须带 `/host/<packId>/` 前缀——与 IPC
 * 通道的 `<packId>:` 前缀同一命名空间纪律。
 */

import type { HttpRouteContribution, HttpRouteRequest } from '../scenario/pack.js';

export type { HttpRouteContribution, HttpRouteRequest };

const packHttpRoutes: HttpRouteContribution[] = [];

function assertPackRoutePath(packId: string, path: string): void {
  if (!path.startsWith(`/host/${packId}/`)) {
    throw new Error(
      `[packs] HTTP 路由 "${path}" 不合规：包 "${packId}" 的路由必须在 /host/${packId}/ 前缀下`,
    );
  }
}

/** 注册包 HTTP 路由；路径越出包前缀直接抛错（组装期失败，不进运行时）。 */
export function registerPackHttpRoutes(
  packId: string,
  routes: readonly HttpRouteContribution[],
): void {
  for (const route of routes) {
    assertPackRoutePath(packId, route.path);
    packHttpRoutes.push(route);
  }
}

/** 测试钩子：清空注册表。 */
export function resetPackHttpRoutes(): void {
  packHttpRoutes.length = 0;
}

/**
 * 匹配已注册的包路由。段参数（`:id`）逐段捕获进 params；无匹配返回 null。
 */
export function matchPackHttpRoute(
  method: string,
  pathname: string,
): { route: HttpRouteContribution; params: Record<string, string> } | null {
  for (const route of packHttpRoutes) {
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
