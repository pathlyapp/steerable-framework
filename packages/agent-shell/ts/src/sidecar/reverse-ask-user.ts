/**
 * W8 ask_user 反向通道：sidecar 内 CoreLoop 的 `ask_user.request` 反向调用
 * 由桌面用户（renderer 问题卡片）应答，返回答案映射。
 *
 * 与审批桥不同，这里 fail-open：无窗口、renderer 抛错、应答畸形都归一为
 * 空 `answers`——模型收到「用户未作答」继续推进，绝不挂起一轮。问题本身
 * 不设桌面侧超时（用户离开多久都可能回来作答，对齐 Claude Code 的阻塞
 * 语义）；sidecar 侧 HostAskUserHandler 自身已对调用异常 fail-open。
 *
 * renderer 经 `ask-user:answer` invoke 回带 requestId 应答。
 */

import { randomUUID } from 'node:crypto';
import type { SidecarReverseHandler } from './types.js';

/** 广播到 renderer 的请示载荷（renderer 原样渲染问题卡片）。 */
export interface AskUserPromptRequest {
  /** 关联 id：renderer 的 `ask-user:answer` 原样回带。 */
  requestId: string;
  intro: string;
  questions: Array<Record<string, unknown>>;
}

interface PendingAskUser {
  resolve: (result: { answers: Record<string, string | string[]> }) => void;
}

export interface AskUserBridgeDeps {
  broadcast: (channel: 'ask-user:request', payload: AskUserPromptRequest) => void;
  /** 是否有能应答的窗口/浏览器连接；没有则立即空答。 */
  hasWindow: () => boolean;
  onLog?: (line: string) => void;
}

export interface AskUserBridge {
  /** 注册为 sidecar 的 `ask_user.request` 反向方法处理器。 */
  handler: SidecarReverseHandler;
  /** renderer `ask-user:answer` 的入口；返回是否匹配到待应答请求。 */
  answer: (payload: unknown) => { ok: boolean };
}

const EMPTY_REPLY = { answers: {} } as const;

export function createAskUserBridge(deps: AskUserBridgeDeps): AskUserBridge {
  const pending = new Map<string, PendingAskUser>();

  return {
    handler: (params) => {
      const p = (params ?? {}) as Partial<AskUserPromptRequest>;
      const questions = Array.isArray(p.questions) ? p.questions : [];
      if (questions.length === 0) {
        deps.onLog?.('ask_user: malformed request (no questions); answering empty');
        return Promise.resolve({ ...EMPTY_REPLY });
      }
      if (!deps.hasWindow()) {
        deps.onLog?.('ask_user: no renderer window; answering empty');
        return Promise.resolve({ ...EMPTY_REPLY });
      }
      const requestId = randomUUID();
      const prompt: AskUserPromptRequest = {
        requestId,
        intro: typeof p.intro === 'string' ? p.intro : '',
        questions: questions as Array<Record<string, unknown>>,
      };
      return new Promise((resolve) => {
        pending.set(requestId, { resolve });
        try {
          deps.broadcast('ask-user:request', prompt);
        } catch (err) {
          pending.delete(requestId);
          deps.onLog?.(`ask_user: broadcast failed: ${String(err)}`);
          resolve({ ...EMPTY_REPLY });
        }
      });
    },

    answer: (payload) => {
      const p = (payload ?? {}) as { requestId?: unknown; answers?: unknown };
      const entry = typeof p.requestId === 'string' ? pending.get(p.requestId) : undefined;
      if (!entry) return { ok: false };
      pending.delete(p.requestId as string);
      const answers =
        p.answers && typeof p.answers === 'object' && !Array.isArray(p.answers)
          ? (p.answers as Record<string, string | string[]>)
          : {};
      entry.resolve({ answers });
      return { ok: true };
    },
  };
}
