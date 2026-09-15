/**
 * ModelPicker 交互测试：
 *   - 目录行渲染 + 选模型（换模型清掉旧档位）；
 *   - 有 reasoningLevels 的模型才出档位选择器；
 *   - 「跟随设置」回到 null（全局设置）；
 *   - offline/stale 状态如实披露（目录不可用 / 缓存），offline 时仍可选择。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GatewayModelCatalog } from '@/lib/local-api';

const getLlmModels = vi.fn();
const getLlmSettings = vi.fn();

vi.mock('@/lib/local-api', () => ({
  getLlmModels: () => getLlmModels(),
  getLlmSettings: () => getLlmSettings(),
}));

const { ModelPicker } = await import('./ModelPicker');

const CATALOG: GatewayModelCatalog = {
  models: [
    {
      id: 'z-ai/glm-5.3-flash',
      name: 'GLM 5.3 Flash',
      window: 131072,
      modalities: ['text'],
      reasoningLevels: ['low', 'high', 'max'],
      pricing: { promptPerMtok: 0.2, completionPerMtok: 0.8 },
      joinedFrom: 'z-ai/glm-5.3-flash',
      capabilities: 'known',
    },
    {
      id: 'openai/qwen/qwen3.8-27b',
      name: null,
      window: null,
      modalities: ['text'],
      reasoningLevels: [],
      pricing: null,
      joinedFrom: null,
      capabilities: 'unknown',
    },
  ],
  catalogStatus: 'live',
  fetchedAt: 1_757_000_000,
  current: { model: null, reasoningEffort: null },
};

function renderPicker(overrides: Partial<Parameters<typeof ModelPicker>[0]> = {}) {
  const props = {
    model: null as string | null,
    reasoningEffort: null as string | null,
    onSelectModel: vi.fn(),
    onSelectEffort: vi.fn(),
    ...overrides,
  };
  const utils = render(<ModelPicker {...props} />);
  return { ...utils, props };
}

describe('ModelPicker', () => {
  beforeEach(() => {
    getLlmModels.mockResolvedValue(CATALOG);
    getLlmSettings.mockResolvedValue({ provider: 'openai-compat', model: 'deepseek-chat' });
  });
  afterEach(() => cleanup());

  it('shows the settings model by default and lists catalog rows', async () => {
    const { props } = renderPicker();
    // 默认展示设置里的模型
    const trigger = await screen.findByText('deepseek-chat');
    fireEvent.click(trigger);

    // 目录行 + 跟随设置行
    expect(await screen.findByText('z-ai/glm-5.3-flash')).toBeTruthy();
    expect(screen.getByText('openai/qwen/qwen3.8-27b')).toBeTruthy();
    expect(screen.getByText(/跟随设置/)).toBeTruthy();
    expect(screen.getByText('思考')).toBeTruthy();
    expect(screen.getByText('131K')).toBeTruthy();
    expect(screen.getByText('未识别')).toBeTruthy();

    fireEvent.click(screen.getByText('z-ai/glm-5.3-flash'));
    expect(props.onSelectModel).toHaveBeenCalledWith('z-ai/glm-5.3-flash');
    // 换模型清档位，让 sidecar 按新模型校验
    expect(props.onSelectEffort).toHaveBeenCalledWith(null);
  });

  it('offers the effort picker only for models with reasoning levels', async () => {
    const { props, rerender } = renderPicker({ model: 'z-ai/glm-5.3-flash' });
    // 等目录到达后档位按钮出现
    const effortTrigger = await screen.findByText('自动');
    fireEvent.click(effortTrigger);
    for (const level of ['low', 'high', 'max']) {
      expect(screen.getByText(level)).toBeTruthy();
    }
    fireEvent.click(screen.getByText('max'));
    expect(props.onSelectEffort).toHaveBeenCalledWith('max');

    // 切到未识别模型：档位选择器消失
    rerender(
      <ModelPicker
        model="openai/qwen/qwen3.8-27b"
        reasoningEffort={null}
        onSelectModel={props.onSelectModel}
        onSelectEffort={props.onSelectEffort}
      />,
    );
    expect(screen.queryByText('自动')).toBeNull();
  });

  it('discloses the offline state and still allows manual selection', async () => {
    getLlmModels.mockResolvedValue({
      models: [],
      catalogStatus: 'offline',
      error: 'connect refused',
    });
    renderPicker();
    const trigger = await screen.findByText('deepseek-chat');
    fireEvent.click(trigger);
    expect(await screen.findByText('目录不可用')).toBeTruthy();
    expect(screen.getByText(/网关不可达/)).toBeTruthy();
  });

  it('badges a stale catalog as cache', async () => {
    getLlmModels.mockResolvedValue({ ...CATALOG, catalogStatus: 'stale' });
    renderPicker();
    fireEvent.click(await screen.findByText('deepseek-chat'));
    expect(await screen.findByText('缓存')).toBeTruthy();
    // 缓存目录的模型行照常可选
    expect(screen.getByText('z-ai/glm-5.3-flash')).toBeTruthy();
  });

  it('omits a leading icon on the trigger', async () => {
    renderPicker({ model: 'z-ai/glm-5.3-flash' });
    const trigger = (await screen.findByText('z-ai/glm-5.3-flash')).closest('button');
    expect(trigger).toBeTruthy();
    expect(screen.getByText('本会话')).toBeTruthy();
    // Chevron only — no CPU / vendor logo in the chip.
    expect(trigger!.querySelectorAll('svg')).toHaveLength(1);
  });

  it('resets to the global settings model via 跟随设置', async () => {
    const { props } = renderPicker({ model: 'z-ai/glm-5.3-flash' });
    fireEvent.click(await screen.findByText('z-ai/glm-5.3-flash'));
    fireEvent.click(await screen.findByText(/跟随设置/));
    expect(props.onSelectModel).toHaveBeenCalledWith(null);
  });
});
