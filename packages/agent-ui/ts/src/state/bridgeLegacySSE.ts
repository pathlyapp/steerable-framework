/**
 * `bridgeLegacySSE` — turn the heterogeneous wire envelopes that deeppath's
 * Next.js API and deeppath-agent's local backend speak into the canonical
 * `SSEEvent` consumed by `useChatStream`.
 *
 * Lifted from:
 *   - deeppath/apps/web/src/lib/utils/framework-sse-bridge.ts (normalize())
 *   - deeppath-agent/apps/web/src/lib/chat-transport.ts (normaliseLocalPayload())
 *
 * The two implementations are merged here, with deeppath-agent's richer
 * `completion` / `executed_actions` / `user_message` / `message_id` handling
 * promoted as the default and the older deeppath wire shapes added as
 * `EnvelopeProfile.deeppathCloud`. A consumer picks a profile (or "auto"),
 * and the bridge does the rest.
 *
 * The bridge is intentionally pure — no I/O, no DOM, no electron — so it can
 * back any transport (fetch SSE, IPC streaming, mocked fixtures, JSON-RPC).
 */

import type { SSEEvent } from '@steerable/agent-protocol';

export type EnvelopeProfile =
  | 'auto'
  /** Next.js `/api/chats/:id/send` legacy wire — `{content}` deltas, `{error}` envelopes. */
  | 'deeppathCloud'
  /** Electron local-backend wire — typed completion/executed_actions/user_message. */
  | 'deeppathLocal';

export interface BridgeLegacySSEOptions {
  profile?: EnvelopeProfile;
  /**
   * If we see a typed event with `type` outside the canonical SSEEvent union
   * (e.g. `stage-complete`, `executed_actions`), repackage it as an `agent`
   * event whose `payload` is the raw envelope. Defaults to `true` so the
   * framework `useChatStream`'s `onUnknownEvent` channel sees them.
   */
  passthroughUnknownAsAgent?: boolean;
}

const KNOWN_EVENT_TYPES = new Set<SSEEvent['type']>([
  'content',
  'error',
  'agent',
  'orchestration',
  'loader-hint',
  'keepalive',
  'done',
  'budget_exhausted',
  'tool_call',
  'tool_result',
]);

/**
 * Convert one parsed envelope (the JSON value of `data:` plus the optional
 * `event:` name) into zero or one `SSEEvent`. Returns null if the envelope is
 * deliberately suppressed (e.g. `user_message` echoes, since useChatStream
 * already appends the user message locally).
 */
export function bridgeLegacySSE(
  parsed: unknown,
  eventName?: string,
  options: BridgeLegacySSEOptions = {},
): SSEEvent | null {
  const profile = options.profile ?? 'auto';
  const passthroughUnknown = options.passthroughUnknownAsAgent ?? true;

  if (typeof parsed === 'string') {
    return parsed.length > 0 ? { type: 'content', content: parsed } : null;
  }
  if (parsed === null || typeof parsed !== 'object') {
    return null;
  }
  const data = parsed as Record<string, unknown>;

  if (eventName === 'error') {
    return {
      type: 'error',
      message: typeof data.message === 'string' ? data.message : 'unknown error',
    };
  }

  // Legacy: `{ content: '...' }` carries an incremental token (deeppath cloud).
  if (typeof data.content === 'string' && !('type' in data)) {
    return { type: 'content', content: data.content };
  }

  // Legacy: `{ error: '...' }` is an inline error envelope (deeppath cloud).
  if (typeof data.error === 'string' && !('type' in data)) {
    return { type: 'error', message: data.error };
  }

  // Canonical typed events.
  if (typeof data.type === 'string') {
    if (KNOWN_EVENT_TYPES.has(data.type as SSEEvent['type'])) {
      return data as unknown as SSEEvent;
    }
    if (profile === 'deeppathLocal' || profile === 'auto') {
      const mapped = mapLocalBackendEnvelope(data);
      if (mapped !== undefined) return mapped;
    }
    if (passthroughUnknown) {
      return { type: 'agent', event: data.type as any, payload: data };
    }
  }
  return null;
}

function mapLocalBackendEnvelope(
  data: Record<string, unknown>,
): SSEEvent | null | undefined {
  switch (data.type as string) {
    case 'completion': {
      const status = data.status as string;
      if (status === 'completed' || status === 'failed') {
        return { type: 'done', payload: data };
      }
      if (status === 'budget_exhausted') {
        return {
          type: 'budget_exhausted',
          payload: {
            limitKind: 'unknown',
            budgetState: {},
          },
          message: typeof data.reason === 'string' ? data.reason : undefined,
        };
      }
      return { type: 'agent', event: 'round_end' as any, payload: data };
    }
    case 'executed_actions':
      return { type: 'agent', event: 'executed_actions' as any, payload: data };
    case 'user_message':
      return null;
    case 'message_id':
      return { type: 'agent', event: 'message_id' as any, payload: data };
    default:
      return undefined;
  }
}
