/**
 * Model-catalog transports for the shell's `ModelSelector`.
 *
 * The selector is transport-agnostic; each host wires its own bridge to the
 * sidecar's `models.list` RPC. The shell demonstrates both ends of the
 * contract:
 *
 *   - mock (default): a canned listing with `catalogStatus: 'live'`, so the
 *     model picker, the reasoning-effort picker, and per-model capabilities
 *     are exercisable fully offline.
 *   - sidecar: `GET {origin}/models` against the same origin that serves
 *     `/chat/stream`, expecting the host to proxy the sidecar's
 *     `models.list` result (`ModelCatalogResponse` JSON). Any failure
 *     degrades to `catalogStatus: 'offline'` — the selector shows the badge
 *     and the composer keeps working, because the catalog is discovery, not
 *     a routing whitelist.
 */
import {
  type ModelCatalogResponse,
  type ModelCatalogTransport,
} from '@steerable/agent-ui';

const MOCK_CATALOG: ModelCatalogResponse = {
  catalogStatus: 'live',
  fetchedAt: 1_789_000_000,
  models: [
    {
      id: 'deepseek-v4-flash',
      name: 'DeepSeek V4 Flash',
      window: 131_072,
      modalities: ['text'],
      reasoningLevels: ['low', 'medium', 'high'],
      pricing: { promptPerMtok: 0.14, completionPerMtok: 0.28 },
      joinedFrom: 'deepseek/deepseek-v4-flash',
      capabilities: 'known',
    },
    {
      id: 'qwen3.8-27b',
      name: 'Qwen 3.8 27B',
      window: 131_072,
      modalities: ['text'],
      reasoningLevels: ['low', 'medium'],
      pricing: { promptPerMtok: 0.1, completionPerMtok: 0.2 },
      joinedFrom: 'qwen/qwen3.8-27b',
      capabilities: 'known',
    },
    {
      id: 'glm-5.3-flash',
      name: 'GLM 5.3 Flash',
      window: 1_048_576,
      modalities: ['text'],
      reasoningLevels: ['low', 'high', 'max'],
      pricing: { promptPerMtok: 0.2, completionPerMtok: 0.6 },
      joinedFrom: 'z-ai/glm-5.3-flash',
      capabilities: 'known',
    },
    {
      id: 'deepseek-v4-chat',
      name: 'DeepSeek V4 Chat (no reasoning knob)',
      window: 131_072,
      modalities: ['text'],
      reasoningLevels: [],
      pricing: null,
      joinedFrom: null,
      capabilities: 'unknown',
    },
  ],
};

export function createMockCatalogTransport(): ModelCatalogTransport {
  return {
    async listModels() {
      return MOCK_CATALOG;
    },
  };
}

export interface SidecarCatalogTransportOptions {
  /** Full chat endpoint, e.g. `http://localhost:5181/chat/stream`. */
  chatEndpoint?: string;
}

export function createSidecarCatalogTransport(
  options: SidecarCatalogTransportOptions = {},
): ModelCatalogTransport {
  const chatEndpoint =
    options.chatEndpoint ||
    (import.meta.env.VITE_SIDECAR_URL as string | undefined) ||
    'http://localhost:5181/chat/stream';
  const origin = new URL(chatEndpoint).origin;

  return {
    async listModels() {
      try {
        const resp = await fetch(`${origin}/models`);
        if (!resp.ok) {
          return {
            models: [],
            catalogStatus: 'offline',
            error: `GET /models answered ${resp.status} ${resp.statusText}`,
          };
        }
        return (await resp.json()) as ModelCatalogResponse;
      } catch (err) {
        return {
          models: [],
          catalogStatus: 'offline',
          error: err instanceof Error ? err.message : String(err),
        };
      }
    },
  };
}
