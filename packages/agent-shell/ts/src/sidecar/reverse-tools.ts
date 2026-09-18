/**
 * A4 reverse channel: serve the sidecar-hosted CoreLoop's `tool.invoke`
 * requests by executing the tool in this process (shell / files / MCP live
 * in Electron, not in Python).
 *
 * Hard-gates critical shell commands — the TS loop only annotates them, but
 * on the CoreLoop path the framework's blocking semantics apply.
 */

import { classifyShellCommand } from './safety-patterns.js';
import { compactToolResultJson } from '../local-backend/context-compactor.js';
import { normalizeToolPolicy } from '../local-backend/agent-capability.js';
import type { ToolRouter } from '../tool-router.js';
import type { SidecarReverseHandler } from './types.js';

export interface ReverseToolDeps {
  toolRouter: ToolRouter;
  onBlocked?: (info: { command: string; rules: string[] }) => void;
  /**
   * 项目模式：按 chatId 解析项目根（绝对路径），无绑定/已删除返回 null。
   * main.ts 注入 LocalBackendRouter.resolveChatProject 的适配；未注入时
   * 项目围栏不生效（CLI/test 场景）。
   */
  resolveProjectRoot?: (chatId: string) => Promise<string | null>;
  /**
   * 额外放行的只读根（会话附件目录等）。写入仍只受 projectRoot 围栏约束，
   * 这里只放宽 local_read_file 的读取范围。
   */
  resolveAdditionalReadRoots?: (chatId: string) => string[];
}

/**
 * 模型上下文硬顶：喂给 sidecar 的工具结果必须待在框架逐项 10k token 断言线
 * 以下（2026-08-28 E2E 录制实锤：local_exec_shell 对 node_modules 跑
 * `grep -R` 返回了 ~68k token——终端采集的 256KiB 上限是 UI 防爆兜底，
 * 不是模型预算）。完整输出用户始终能在可见终端里看到，这里只截断喂给
 * 模型的那份。复用 context-compactor 的既有策略（8000 字符总量 /
 * 2000 单字段，最坏情况纯 CJK 约 4.8k token）。
 */
function capResultForModelContext(out: unknown): unknown {
  const json = JSON.stringify(out);
  if (json.length <= 8000) return out;
  return JSON.parse(compactToolResultJson(json));
}

export function createToolInvokeHandler(deps: ReverseToolDeps): SidecarReverseHandler {
  return async (params) => {
    const p = (params ?? {}) as {
      name?: string;
      arguments?: Record<string, unknown>;
      context?: {
        mode?: string;
        chatId?: string;
        workspaceRoot?: string;
        toolPolicy?: unknown;
      } | null;
    };
    const name = typeof p.name === 'string' ? p.name : '';
    const toolArgs = p.arguments ?? {};
    if (!name) {
      return { success: false, error: 'missing tool name', needsFollowup: false };
    }
    // Plan mode is enforced at the execution layer too, not just by tool
    // advertisement — a pseudo-recovered call could name a write tool that
    // was never advertised.
    if (p.context?.mode === 'plan') {
      const schema = deps.toolRouter.listSchemas().find((s) => s.name === name);
      if (schema && schema.mode !== 'read') {
        return {
          success: false,
          error: `blocked by plan mode: ${name} is a write tool`,
          needsFollowup: false,
        };
      }
    }
    // 项目模式硬围栏（W4-2 接线补齐）：CoreLoop 路径此前把 projectRoot 丢
    // 了——toolContext 只带 mode，ToolRouter 的项目沙箱（cwd 收口 + 文件
    // 路径围栏）在 sidecar 驱动的回合里形同虚设。这里从 chat 绑定解析
    // 项目根并随调用上下文下发，与旧 TS 循环语义对齐。
    // 4.6b：调用方显式带 workspaceRoot（worktree 任务的隔离工作区）时
    // 优先采用——围栏根收窄到 worktree，任务摸不到主检出。
    const projectRoot =
      typeof p.context?.workspaceRoot === 'string' && p.context.workspaceRoot
        ? p.context.workspaceRoot
        : p.context?.chatId
          ? (await deps.resolveProjectRoot?.(p.context.chatId) ?? null)
          : null;
    const additionalReadRoots = p.context?.chatId
      ? (deps.resolveAdditionalReadRoots?.(p.context.chatId) ?? [])
      : [];
    if (name === 'local_exec_shell') {
      const classification = classifyShellCommand(String(toolArgs.command ?? ''));
      if (classification.severity === 'critical') {
        deps.onBlocked?.({
          command: String(toolArgs.command ?? '').slice(0, 200),
          rules: classification.matchedRules,
        });
        return {
          success: false,
          error: `blocked by shell safety policy (critical): ${classification.matchedRules.join(', ')}`,
          needsFollowup: false,
        };
      }
    }
    // 智能体工具策略随 toolContext 下发（router 按本轮生效的智能体解析），
    // 未配置策略时不进调用上下文——那种回合的上下文与旧版逐字段一致。
    const normalizedPolicy = p.context?.toolPolicy
      ? normalizeToolPolicy(p.context.toolPolicy)
      : null;
    const toolPolicy =
      normalizedPolicy && normalizedPolicy.mode !== 'all' ? normalizedPolicy : null;
    const execContext =
      projectRoot || additionalReadRoots.length > 0 || p.context?.chatId || toolPolicy
        ? {
            projectRoot,
            chatId: p.context?.chatId,
            // 4.6a：chatId 随上下文透传——task_run/status/result 与
            // worktree_* 工具按它把任务绑定到来源对话。
            ...(toolPolicy ? { toolPolicy } : {}),
            ...(additionalReadRoots.length > 0 ? { additionalReadRoots } : {}),
          }
        : undefined;
    try {
      const out = await deps.toolRouter.execute(
        { name, arguments: toolArgs },
        execContext,
      );
      if (out && typeof out === 'object' && 'success' in (out as Record<string, unknown>)) {
        return capResultForModelContext(out);
      }
      return capResultForModelContext({ success: true, data: { value: out } });
    } catch (err) {
      return {
        success: false,
        error: err instanceof Error ? err.message : String(err),
        needsFollowup: true,
      };
    }
  };
}
