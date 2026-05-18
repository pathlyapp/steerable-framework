/**
 * Optional sidecar transport. Activates when the consumer sets
 * `VITE_TRANSPORT=sidecar` on `pnpm dev`. The transport speaks the same
 * JSON-RPC + SSE protocol as `examples/sidecar-roundtrip` so we don't ship a
 * separate backend binary -- you point it at a running sidecar on
 * `http://localhost:5181` (or override with `VITE_SIDECAR_URL`).
 *
 * To run end-to-end:
 *
 *   # terminal 1
 *   pnpm --filter steerable-example-sidecar-roundtrip dev
 *
 *   # terminal 2
 *   VITE_TRANSPORT=sidecar pnpm --filter steerable-example-web-shell dev
 */
import {
  SSEParser,
  bridgeLegacySSE,
  type ChatStreamTransport,
  type ChatStreamSendInput,
} from '@steerable/agent-ui';
import type { SSEEvent } from '@steerable/agent-protocol';

export interface SidecarTransportOptions {
  endpoint?: string;
}

export function createSidecarTransport(options: SidecarTransportOptions = {}): ChatStreamTransport {
  const endpoint =
    options.endpoint ||
    (import.meta.env.VITE_SIDECAR_URL as string | undefined) ||
    'http://localhost:5181/chat/stream';

  return {
    async stream(input: ChatStreamSendInput, onEvent) {
      const controller = new AbortController();
      const resp = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: input.content, metadata: input.metadata ?? null }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`Sidecar transport failed: ${resp.status} ${resp.statusText}`);
      }
      const parser = new SSEParser({
        onFrame: (frame) => {
          if (frame.data === undefined) return;
          let bridged: SSEEvent | null;
          try {
            const parsed = JSON.parse(frame.data);
            bridged = bridgeLegacySSE(parsed, frame.event);
          } catch {
            return;
          }
          if (bridged) onEvent(bridged);
        },
      });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder('utf-8');
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          parser.feed(decoder.decode(value, { stream: true }));
        }
        parser.end();
      } finally {
        try {
          reader.releaseLock();
        } catch {
          /* noop */
        }
      }

      return () => controller.abort();
    },
  };
}
