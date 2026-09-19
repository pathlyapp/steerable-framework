/**
 * Local chat transport — bridges Electron IPC SSE -> `@steerable/agent-ui`'s
 * `ChatStreamTransport` contract.
 *
 * Pipeline:
 *
 *   user types in ChatPanel
 *     -> useChatStream.sendUserMessage()
 *     -> transport.stream(input, onEvent)
 *     -> window.electron.localBackend.startStream({ POST /api/v2/chats/:id/run })
 *     -> main process forwards each raw SSE chunk via webContents.send
 *     -> preload re-emits as `{ type: 'data'; chunk: string }`
 *     -> we parse the SSE frames with the framework's `SSEParser`, normalise
 *        the payload via `bridgeLegacySSE`, and call `onEvent` for each
 *        normalised event
 *     -> useChatStream's reducer renders messages
 *
 * Local-backend's wire format (see src/local-backend/router.ts handleStream):
 *
 *   - Standard content tokens:    `data: {"content":"..."}\n\n`
 *   - User message echo:          `data: {"type":"user_message", "message":{...}}\n\n`
 *   - Tool execution summary:     `data: {"type":"executed_actions", "actions":[...]}\n\n`
 *   - Budget exhausted:           `data: {"type":"budget_exhausted", "budget":{"kind":"..."}, "message":"..."}\n\n`
 *   - Persisted message id:       `data: {"type":"message_id", "messageId":"..."}\n\n`
 *   - Named error event:          `event: error\ndata: {"message":"..."}\n\n`
 *   - Stream terminator:          `data: [DONE]\n\n`
 *
 * Framework `SSEEvent` (packages/agent-protocol/ts/src/generated/SSEEvent.ts)
 * is the canonical normalisation target. The legacy-envelope mapping lives in
 * `@steerable/agent-ui/state`'s `bridgeLegacySSE` — this file used to keep its
 * own copy; wave 1 of the steerable-framework refactor consolidated them.
 *
 * We do still keep one app-specific guard: if any content has already
 * streamed and the backend ends with `budget_exhausted`, we downgrade that
 * event to a generic `agent/round_end` + `done` so the framework hook
 * doesn't blow away the already-rendered text via patch-last-assistant.
 *
 * Open issue (upstream): framework `useChatStream`'s cancel pathway stores
 * the cancel handle only AFTER `await transport.stream(...)` resolves, by
 * which point the stream has already ended. So the framework's Stop button
 * is effectively a no-op today. We still return a cancel function from the
 * resolved promise per the contract; full cancel UX needs an upstream fix.
 */

import type {
  ChatStreamTransport,
  ChatStreamSendInput,
} from '@steerable/agent-ui';
import {
  SSEParser,
  bridgeLegacySSE,
  parseSSEData,
} from '@steerable/agent-ui/state';
import type { SSEEvent } from '@steerable/agent-protocol';
import { getElectronBridge } from './electron-bridge';
import { appendDelta, sealLastBlock, syncTools, type TurnBlock } from '@/components/chat/turn-timeline';
import type { ExecutedAction } from '@/components/chat/ExecutedActionsCard';

// ---------------------------------------------------------------------------
// Local-backend SSE adapter — feeds opaque IPC chunks into the framework's
// `SSEParser`, then runs every frame through `bridgeLegacySSE` and applies a
// small app-specific guard around `budget_exhausted` (see file header).
// ---------------------------------------------------------------------------

class LocalBackendSseAdapter {
  /** True once any content delta has been forwarded for this stream. */
  private hasStreamedContent = false;
  private completed = false;
  private readonly parser: SSEParser;
  private timeline: TurnBlock[] = [];

  constructor(private readonly onEvent: (event: SSEEvent) => void) {
    this.parser = new SSEParser({
      onFrame: (frame) => {
        if (!frame.data) return;
        const parsed = parseSSEData(frame.data);
        const ev = bridgeLegacySSE(parsed, frame.event);
        if (ev) this.handleNormalised(ev);
      },
      onComplete: () => {
        if (!this.completed) {
          this.completed = true;
          this.onEvent({ type: 'done' });
        }
      },
    });
  }

  feed(chunk: string): void {
    this.parser.feed(chunk);
  }

  end(): void {
    this.parser.end();
    if (!this.completed) {
      this.completed = true;
      this.onEvent({ type: 'done' });
    }
  }

  private handleNormalised(event: SSEEvent): void {
    if (event.type === 'content' && typeof event.content === 'string' && event.content.length > 0) {
      this.hasStreamedContent = true;
    }
    if (event.type === 'budget_exhausted' && this.hasStreamedContent) {
      this.onEvent({
        type: 'agent',
        event: 'budget_exhausted_suppressed',
        payload: { reason: event.message ?? 'budget_exhausted' },
      } as any);
      if (!this.completed) {
        this.completed = true;
        this.onEvent({ type: 'done' });
      }
      return;
    }
    const timelineChanged = this.applyToTimeline(event);
    this.onEvent(event);
    if (timelineChanged) {
      this.onEvent({
        type: 'agent',
        event: 'turn_timeline',
        payload: { blocks: this.timeline },
      });
    }
  }

  private applyToTimeline(event: SSEEvent): boolean {
    if (event.type === 'content' && typeof event.content === 'string' && event.content.length > 0) {
      this.timeline = appendDelta(this.timeline, 'text', event.content);
      return true;
    }
    if (event.type !== 'agent') return false;
    if (event.event === 'reasoning') {
      const delta =
        typeof event.payload?.content === 'string'
          ? event.payload.content
          : typeof event.payload?.delta === 'string'
            ? event.payload.delta
            : '';
      if (!delta) return false;
      this.timeline = appendDelta(this.timeline, 'reasoning', delta);
      return true;
    }
    if (event.event === 'executed_actions') {
      const actions = event.payload?.actions as ExecutedAction[] | undefined;
      if (!Array.isArray(actions)) return false;
      this.timeline = syncTools(this.timeline, actions);
      return true;
    }
    if (event.event === 'round_end') {
      if (event.payload?.status === 'cancelled') return false;
      const next = sealLastBlock(this.timeline);
      if (next === this.timeline) return false;
      this.timeline = next;
      return true;
    }
    return false;
  }
}

// ---------------------------------------------------------------------------
// Transport factory — bind to a specific chatId. Each `stream()` invocation
// starts a fresh SSE pipe against /api/v2/chats/:id/run.
// ---------------------------------------------------------------------------

export interface ElectronChatTransport extends ChatStreamTransport {
  /**
   * 立即取消当前进行中的流（如果有）。
   *
   * 框架的 `useChatStream` 只在 `transport.stream()` resolve 之后才保存
   * cancel 句柄——那时流已经结束，Stop 按钮等于空操作。这个方法绕过该缺陷：
   * transport 自己记住 in-flight streamId，UI 层（Stop 按钮 / 组件卸载）
   * 直接调它把主进程里的 agent 循环真正停下来。
   */
  cancelActive: () => void;
}

export function createElectronChatTransport(chatId: string): ElectronChatTransport {
  // 当前 in-flight 流的 id。startStream resolve 后写入，流结束（end/error）
  // 或被取消后清空。同一 transport 一次只会有一条流（useChatStream 在
  // isStreaming 时拒绝重入）。
  const active: { streamId: string | null } = { streamId: null };

  return {
    cancelActive: () => {
      const bridge = getElectronBridge();
      if (active.streamId) {
        bridge?.localBackend.cancelStream(active.streamId);
        active.streamId = null;
        return;
      }
      // 切走再切回后：当前 mount 没有原 streamId（它属于上一个已卸载的
      // mount），改用 chatId 取消后端仍在跑的回合。fire-and-forget。
      if (bridge) {
        void bridge.localBackend
          .request({
            method: 'POST',
            path: `/api/v2/chats/${encodeURIComponent(chatId)}/cancel`,
            body: {},
          })
          .catch(() => {});
      }
    },
    // 轮中转向：仅在 CoreLoop 路径（STEERABLE_USE_CORELOOP=1）且回合仍
    // 运行时生效；否则主进程软失败，useChatStream 保留草稿。
    steer: async (content: string) => {
      const bridge = getElectronBridge();
      if (!bridge?.localBackend.steerChat) return false;
      return await bridge.localBackend.steerChat(chatId, content);
    },
    stream: async (input: ChatStreamSendInput, onEvent) => {
      const bridge = getElectronBridge();
      if (!bridge) {
        throw new Error(
          'Electron bridge unavailable — chat transport requires the desktop shell.',
        );
      }

      const adapter = new LocalBackendSseAdapter(onEvent);

      // We mutate this from inside the IPC callback (settle) and read it from
      // the cancel handle returned at the end. Declaring it up-front sidesteps
      // any temporal-dead-zone surprises.
      const ref: { streamId: string | null } = { streamId: null };

      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const settle = (err?: Error) => {
          if (settled) return;
          settled = true;
          if (err) reject(err);
          else resolve();
        };

        bridge.localBackend
          .startStream(
            {
              method: 'POST',
              path: `/api/v2/chats/${encodeURIComponent(chatId)}/run`,
              body: {
                message: input.content,
                ...(input.metadata ?? {}),
              },
            },
            (payload) => {
              if (payload.type === 'data') {
                adapter.feed(payload.chunk);
                return;
              }
              if (payload.type === 'end') {
                active.streamId = null;
                adapter.end();
                settle();
                return;
              }
              // payload.type === 'error'
              active.streamId = null;
              adapter.end();
              onEvent({ type: 'error', message: payload.error });
              settle(new Error(payload.error));
            },
          )
          .then((streamId) => {
            ref.streamId = streamId;
            active.streamId = streamId;
          })
          .catch((err: unknown) => {
            const msg = err instanceof Error ? err.message : String(err);
            onEvent({ type: 'error', message: msg });
            settle(new Error(msg));
          });
      });

      // Return cancel handle per the transport contract. Note: framework's
      // hook stores this AFTER stream completion, so it's a no-op in practice.
      // Real cancel-while-streaming goes through `cancelActive()` above.
      return () => {
        if (ref.streamId) bridge.localBackend.cancelStream(ref.streamId);
      };
    },
  };
}

// Exported for unit testing the SSE adapter separately from the IPC bridge.
export const __test__ = {
  LocalBackendSseAdapter,
};

/**
 * W1.2.1: regenerate an assistant turn via the backend's regenerate route
 * (fork-preserving truncate-and-rerun). The stream's events are not fed to
 * `useChatStream` — the hook's message state is thrown away on the
 * re-hydrate that follows anyway, so this just runs the turn to completion
 * and lets the caller remount the message list from the store.
 */
export async function regenerateChatMessage(
  chatId: string,
  messageId: string,
): Promise<void> {
  const bridge = getElectronBridge();
  if (!bridge) {
    throw new Error(
      'Electron bridge unavailable — regenerate requires the desktop shell.',
    );
  }
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const settle = (err?: Error) => {
      if (settled) return;
      settled = true;
      if (err) reject(err);
      else resolve();
    };
    // The route can refuse before running anything — it declines to regenerate
    // when the old reply could not be forked into a branch. A refusal arrives
    // as a named `error` frame plus a non-2xx `end`; the main process only
    // sends `type: 'error'` when the handler throws, so without reading the
    // frame the caller would see an ordinary completion.
    let refusal: string | null = null;
    const parser = new SSEParser({
      onFrame: (frame) => {
        if (frame.event !== 'error' || !frame.data) return;
        const parsed = parseSSEData(frame.data);
        const message = (parsed as { message?: unknown } | null)?.message;
        refusal = typeof message === 'string' && message ? message : '重新生成失败';
      },
    });
    bridge.localBackend
      .startStream(
        {
          method: 'POST',
          path: `/api/v2/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(messageId)}/regenerate`,
          body: {},
        },
        (payload) => {
          if (payload.type === 'data') parser.feed(payload.chunk);
          else if (payload.type === 'end') {
            parser.end();
            settle(
              payload.status >= 400
                ? new Error(refusal ?? `重新生成失败（HTTP ${payload.status}）`)
                : undefined,
            );
          } else if (payload.type === 'error') settle(new Error(payload.error));
        },
      )
      .catch((err: unknown) => {
        settle(err instanceof Error ? err : new Error(String(err)));
      });
  });
}
