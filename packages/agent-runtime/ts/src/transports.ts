/**
 * Adapters that let `@steerable/agent-ui` hooks consume an `AgentRuntime`
 * directly — `useChatStream` gets a `ChatStreamTransport`, `useAgentSession`
 * gets an `AgentSessionTransport`.
 *
 * The interfaces below are declared locally and kept structurally identical
 * to the ones in `@steerable/agent-ui/hooks`; the runtime package must not
 * depend on the UI package (layering: UI sits above runtime). The
 * compile-time gate `test/ui-transport.types.ts` asserts assignability both
 * ways so the two declarations cannot drift apart.
 */

import type { AgentSession, SSEEvent } from '@steerable/agent-protocol';
import type { AgentRuntime, ChatStreamParams } from './runtime.js';

/** Mirrors `@steerable/agent-ui` `ChatStreamSendInput`. */
export interface ChatStreamSendInput {
  content: string;
  metadata?: Record<string, unknown>;
}

/** Mirrors `@steerable/agent-ui` `ChatStreamTransport`. */
export interface ChatStreamTransport {
  stream: (
    input: ChatStreamSendInput,
    onEvent: (event: SSEEvent) => void,
  ) => Promise<void | (() => void)>;
  steer?: (content: string) => Promise<boolean>;
}

/** Mirrors `@steerable/agent-ui` `AgentSessionTransport`. */
export interface AgentSessionTransport {
  create: (input: {
    chatId: string;
    userId: string;
    projectId?: string | null;
    scenario?: string;
    stageData?: Record<string, unknown> | null;
  }) => Promise<AgentSession>;
  resume: (sessionId: string) => Promise<AgentSession>;
  list: (filter: {
    userId?: string;
    chatId?: string;
    activeOnly?: boolean;
  }) => Promise<AgentSession[]>;
}

export interface ChatStreamTransportOptions {
  /**
   * Static portion of every `agent.chat.stream` request — must pin at least
   * the provider and model; any other sidecar field is passed through.
   * (Written as Pick+Record instead of Omit: Omit collapses the index
   * signature on ChatStreamParams and would drop the required fields.)
   */
  params: Pick<ChatStreamParams, 'provider' | 'model'> & Record<string, unknown>;
  /**
   * Resolve the session a turn belongs to. Called once per `stream` call;
   * typically `() => currentSessionId` from your session hook. The value is
   * forwarded as the request's `sessionId` passthrough field — session
   * binding itself is the host's job (`agent.session.create`/`resume`).
   */
  sessionId: () => string;
}

/**
 * Build the `ChatStreamTransport` for `useChatStream`. Per the hook's
 * contract, `stream` resolves when the turn terminates; mid-turn stop is
 * `runtime.cancelChat(streamId)` (cooperative — the loop winds down at the
 * next safe point and the terminal `done` still arrives), and `steer`
 * targets the currently active stream.
 */
export function createChatStreamTransport(
  runtime: AgentRuntime,
  options: ChatStreamTransportOptions,
): ChatStreamTransport {
  let activeStreamId: string | null = null;
  return {
    stream: async (input, onEvent) => {
      const handle = await runtime.chatStream({
        ...options.params,
        messages: [{ role: 'user', content: input.content }],
        sessionId: options.sessionId(),
        metadata: input.metadata,
      });
      activeStreamId = handle.streamId;
      try {
        for await (const event of handle.events) {
          onEvent(event);
        }
      } finally {
        if (activeStreamId === handle.streamId) activeStreamId = null;
      }
    },
    steer: async (content) => {
      if (!activeStreamId) return false;
      return runtime.steerChat(activeStreamId, content);
    },
  };
}

/**
 * Build the `AgentSessionTransport` for `useAgentSession` — session
 * lifecycle straight onto the sidecar's storage adapter.
 */
export function createSessionTransport(
  runtime: AgentRuntime,
): AgentSessionTransport {
  return {
    create: (input) => runtime.createSession(input),
    resume: (sessionId) => runtime.resumeSession(sessionId),
    list: (filter) => runtime.listSessions(filter),
  };
}
