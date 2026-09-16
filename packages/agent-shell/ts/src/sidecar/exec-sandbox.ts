/**
 * Layer-3（逐 exec 沙箱）的桌面态势：`execSandbox` 请求参数的唯一构造点。
 *
 * 聊天回合（router.ts）与任务回合（task-service.ts）发的是同一份态势，只有
 * 可写根不同。两边各建一份的时候，`requireFull` 在其中一边改错就只在另一边
 * 暴露——所以这里收成一个 `buildExecSandbox`。
 */
import log from 'electron-log';
import { llmService } from '../llm/index.js';
import { getActiveEgressProxyEndpoint } from './egress-proxy.js';
import type { SidecarSupervisor } from './supervisor.js';
import type { SidecarChatStreamRequest } from './types.js';

export type ExecSandboxEnforcement = 'full' | 'partial' | 'none';

/** 会话级命令沙箱档：工作区围栏 vs 关闭逐条命令沙箱。 */
export type ExecPolicy = 'workspace' | 'full';

export function parseExecPolicy(value: unknown): ExecPolicy {
  return value === 'full' ? 'full' : 'workspace';
}

export interface ExecSandboxCapability {
  backend: string;
  enforcement: ExecSandboxEnforcement;
}

/**
 * 一个回合发出的 layer-3 参数。字段全必填——请求类型把它们都设成可选是为了
 * 让宿主省略整段，而桌面永远发全套，`worldState` 的权限披露也直接读这里。
 */
export type ExecSandboxParams =
  Required<Omit<NonNullable<SidecarChatStreamRequest['execSandbox']>, 'allowedHosts'>>
  & { allowedHosts: string[] | undefined };

/**
 * W4-2/W4-3：从 provider baseUrl 派生出egress 允许列表条目——代理未启用时
 * agent 唯一合法的出网对端。baseUrl 缺失/不可解析时返回 undefined（沙箱保持
 * 出网开放；加固永远不能弄断 LLM 通路）。与 layer-1 同样的 sbpl 限制：远端
 * 条目退化为端口级强制。
 */
export function deriveEgressAllowListFromBaseUrl(
  baseUrl: string | undefined,
): string[] | undefined {
  if (!baseUrl) return undefined;
  try {
    const url = new URL(baseUrl);
    const host = url.hostname;
    if (!host) return undefined;
    return [url.port ? `${host}:${url.port}` : host];
  } catch {
    return undefined;
  }
}

let capability: ExecSandboxCapability | null = null;

export function getExecSandboxCapability(): ExecSandboxCapability | null {
  return capability;
}

/**
 * 用回合真实会发的 egress 参数问 sidecar：这台机器的后端到底能达到哪一档
 * 强制。boot 时问一次即可——后端选择只随平台与 egress 变化，两者在一次进程
 * 生命周期内都是定的。
 *
 * 探测失败按「未知能力」处理（保持 null），`buildExecSandbox` 于是不要求
 * full。宁可少收紧一档，也不能因为一次 RPC 抖动把整个 shell 面判死。
 */
export async function probeExecSandboxCapability(
  supervisor: SidecarSupervisor,
): Promise<void> {
  const endpoint = getActiveEgressProxyEndpoint();
  try {
    capability = await supervisor.call<ExecSandboxCapability>('sandbox.describe', {
      network: true,
      allowedHosts: endpoint
        ? [endpoint]
        : deriveEgressAllowListFromBaseUrl(llmService.getSettings().baseUrl),
    });
    log.info(
      `[sidecar] exec sandbox: ${capability.backend} → ${capability.enforcement}`,
    );
  } catch (err) {
    capability = null;
    log.warn('[sidecar] exec sandbox probe failed; not requiring full enforcement', err);
  }
}

/**
 * 一个回合的 `execSandbox` 参数。
 *
 * `requireFull` 只在探测确认这台机器真能达到 full 时才开。它会在执行前拒掉
 * 所有达不到 full 的调用，而带 egress 时只有 Seatbelt 能报 full（它按主机
 * 钉死）——bwrap 与 Landlock 没有按主机管控、Windows 没有改写后端。从平台
 * 猜测推导过一次，结果是代理一开、Linux 上每一次 shell 调用都被拒。
 *
 * `requireBackend` 在沙箱开启时始终开：请求了收容就不能因为没有后端而裸跑。
 * `policy: 'full'` 关闭这一层（enabled/requireBackend 都为 false）。
 */
export function buildExecSandbox(
  writableRoots: string[],
  options?: { policy?: ExecPolicy },
): ExecSandboxParams {
  const policy = options?.policy ?? 'workspace';
  const enabled =
    policy !== 'full' && process.env.STEERABLE_EXEC_SANDBOX !== '0';
  const endpoint = getActiveEgressProxyEndpoint();
  return {
    enabled,
    writableRoots: enabled ? writableRoots : [],
    network: true,
    // P3.2 shell-via-proxy：代理活跃时把 shell 出网收敛到本机代理端点
    // （HTTP(S) 走代理的 CONNECT 名单；SSH 这类非 HTTP 出网由内核 fail-closed
    // 拒掉）。代理未启用时退回 baseUrl 派生的远端条目。
    allowedHosts: endpoint
      ? [endpoint]
      : deriveEgressAllowListFromBaseUrl(llmService.getSettings().baseUrl),
    requireFull: enabled && capability?.enforcement === 'full',
    requireBackend: enabled,
    // W4.1.1：Windows 没有命令改写器——把 shell 调用路由到宿主的受限
    // spawn（win-spawn-helper：受限令牌 + Job Object），而不是以
    // enforcement:"none" 裸跑。其他平台无害：解析到本地后端就不会走这条。
    hostSpawn: enabled && process.platform === 'win32',
  };
}
