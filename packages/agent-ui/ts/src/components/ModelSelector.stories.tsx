import type { Meta, StoryObj } from '@storybook/react';
import { useState } from 'react';
import { ModelSelector } from './ModelSelector';
import type {
  ModelCatalogResponse,
  ModelCatalogTransport,
} from './ModelSelector';

/**
 * `ModelSelector` against canned transports: the live gateway catalog, the
 * stale-cache degradation, and the offline state (gateway unreachable — the
 * composer stays usable, the picker degrades to a badge).
 */
const meta: Meta<typeof ModelSelector> = {
  title: 'Components/ModelSelector',
  component: ModelSelector,
};
export default meta;

type Story = StoryObj<typeof ModelSelector>;

const LIVE_CATALOG: ModelCatalogResponse = {
  models: [
    {
      id: 'openai/deepseek/deepseek-v4-flash',
      name: 'DeepSeek V4 Flash',
      window: 1_048_576,
      modalities: ['text'],
      reasoningLevels: ['low', 'high'],
      pricing: { promptPerMtok: 0.14, completionPerMtok: 0.28 },
      joinedFrom: 'openrouter/deepseek/deepseek-v4-flash',
      capabilities: 'known',
    },
    {
      id: 'openai/qwen/qwen3.8-27b',
      name: 'Qwen 3.8 27B',
      window: 262_144,
      modalities: ['text', 'image'],
      reasoningLevels: ['low', 'medium'],
      pricing: { promptPerMtok: 0.2, completionPerMtok: 0.8 },
      joinedFrom: 'openrouter/qwen/qwen3.8-27b',
      capabilities: 'known',
    },
    {
      id: 'openai/z-ai/glm-5.3-flash',
      name: 'GLM 5.3 Flash',
      window: 1_048_576,
      modalities: ['text', 'image'],
      reasoningLevels: ['low', 'high', 'max'],
      pricing: null,
      joinedFrom: 'openrouter/z-ai/glm-5.3-flash',
      capabilities: 'known',
    },
    {
      id: 'openai/acme-internal-1',
      name: 'openai/acme-internal-1',
      window: 60_000,
      modalities: ['text'],
      reasoningLevels: [],
      pricing: null,
      joinedFrom: null,
      capabilities: 'unknown',
    },
  ],
  catalogStatus: 'live',
  fetchedAt: 1_757_000_000,
  current: { model: 'openai/qwen/qwen3.8-27b', reasoningEffort: null },
};

function transportOf(
  impl: ModelCatalogTransport['listModels'],
): ModelCatalogTransport {
  return { listModels: impl };
}

function Demo({ catalog }: { catalog: ModelCatalogResponse }) {
  const [model, setModel] = useState('openai/qwen/qwen3.8-27b');
  const [effort, setEffort] = useState<string | null>(null);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <ModelSelector
        transport={transportOf(async () => catalog)}
        model={model}
        reasoningEffort={effort}
        onSelectModel={(id) => {
          setModel(id);
          setEffort(null);
        }}
        onSelectEffort={setEffort}
      />
      <div style={{ fontSize: 12, opacity: 0.7 }}>
        selection: {model} / {effort ?? 'default'}
      </div>
    </div>
  );
}

export const Live: Story = {
  render: () => <Demo catalog={LIVE_CATALOG} />,
};

export const Stale: Story = {
  render: () => <Demo catalog={{ ...LIVE_CATALOG, catalogStatus: 'stale' }} />,
};

export const Offline: Story = {
  render: () => (
    <Demo
      catalog={{
        models: [],
        catalogStatus: 'offline',
        error: 'gateway catalog fetch failed: connection refused',
      }}
    />
  ),
};
