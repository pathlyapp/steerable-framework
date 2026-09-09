import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ModelSelector } from './ModelSelector';
import type {
  ModelCatalogResponse,
  ModelCatalogTransport,
} from './ModelSelector';

const CATALOG: ModelCatalogResponse = {
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
      pricing: null,
      joinedFrom: 'openrouter/qwen/qwen3.8-27b',
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
  return { listModels: vi.fn(impl) };
}

describe('ModelSelector', () => {
  it('lists the gateway catalog and reports the selection', async () => {
    const onSelectModel = vi.fn();
    render(
      <ModelSelector
        transport={transportOf(async () => CATALOG)}
        model="openai/qwen/qwen3.8-27b"
        onSelectModel={onSelectModel}
      />,
    );

    const select = await screen.findByRole('combobox', { name: 'Model' });
    expect((select as HTMLSelectElement).value).toBe('openai/qwen/qwen3.8-27b');
    expect(screen.getByText('openai/deepseek/deepseek-v4-flash')).toBeTruthy();

    fireEvent.change(select, {
      target: { value: 'openai/deepseek/deepseek-v4-flash' },
    });
    expect(onSelectModel).toHaveBeenCalledWith('openai/deepseek/deepseek-v4-flash');
  });

  it('offers the joined reasoning levels for the selected model', async () => {
    const onSelectEffort = vi.fn();
    render(
      <ModelSelector
        transport={transportOf(async () => CATALOG)}
        model="openai/qwen/qwen3.8-27b"
        reasoningEffort={null}
        onSelectModel={() => {}}
        onSelectEffort={onSelectEffort}
      />,
    );

    const effort = await screen.findByRole('combobox', {
      name: 'Reasoning effort',
    });
    const options = Array.from(effort.querySelectorAll('option')).map(
      (option) => option.value,
    );
    expect(options).toEqual(['', 'low', 'medium']);

    fireEvent.change(effort, { target: { value: 'medium' } });
    expect(onSelectEffort).toHaveBeenCalledWith('medium');
  });

  it('hides the effort picker for models without a reasoning knob', async () => {
    render(
      <ModelSelector
        transport={transportOf(async () => CATALOG)}
        model="openai/acme-internal-1"
        onSelectModel={() => {}}
        onSelectEffort={() => {}}
      />,
    );

    await screen.findByRole('combobox', { name: 'Model' });
    expect(
      screen.queryByRole('combobox', { name: 'Reasoning effort' }),
    ).toBeNull();
  });

  it('keeps an unlisted current model selectable (discovery, not whitelist)', async () => {
    render(
      <ModelSelector
        transport={transportOf(async () => CATALOG)}
        model="openai/custom-fine-tune"
        onSelectModel={() => {}}
      />,
    );

    const select = await screen.findByRole('combobox', { name: 'Model' });
    expect((select as HTMLSelectElement).value).toBe('openai/custom-fine-tune');
  });

  it('shows the offline state and disables the picker when the catalog is unreachable', async () => {
    render(
      <ModelSelector
        transport={transportOf(async () => ({
          models: [],
          catalogStatus: 'offline',
          error: 'connection refused',
        }))}
        model="openai/qwen/qwen3.8-27b"
        onSelectModel={() => {}}
      />,
    );

    await screen.findByText('catalog offline');
    const select = screen.getByRole('combobox', { name: 'Model' });
    expect((select as HTMLSelectElement).disabled).toBe(true);
    // The configured model is still displayed, not blanked.
    expect((select as HTMLSelectElement).value).toBe('openai/qwen/qwen3.8-27b');
  });

  it('degrades to offline when the transport itself fails', async () => {
    render(
      <ModelSelector
        transport={transportOf(async () => {
          throw new Error('sidecar gone');
        })}
        model="openai/qwen/qwen3.8-27b"
        onSelectModel={() => {}}
      />,
    );

    await screen.findByText('catalog offline');
  });

  it('marks a stale listing without disabling selection', async () => {
    render(
      <ModelSelector
        transport={transportOf(async () => ({
          ...CATALOG,
          catalogStatus: 'stale' as const,
        }))}
        model="openai/qwen/qwen3.8-27b"
        onSelectModel={() => {}}
      />,
    );

    await screen.findByText('stale');
    await waitFor(() =>
      expect(
        (screen.getByRole('combobox', { name: 'Model' }) as HTMLSelectElement)
          .disabled,
      ).toBe(false),
    );
  });
});
