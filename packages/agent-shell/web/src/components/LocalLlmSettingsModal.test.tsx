/**
 * LocalLlmSettingsModal 的厂商参数预制交互（R16 配置入口）：
 *   - openai-compat 显示预制区，ollama 不显示；
 *   - 「自动」档按 baseUrl+model 向框架 resolve 并预览命中参数；
 *   - 「关闭」保存 {enabled:false}；「指定预制」下拉选中后保存 override；
 *   - Temperature 自动/手动切换：自动 = 不下发（undefined），手动滑杆持久化显式值。
 * 进程/wire 层由 tests/e2e/presets.e2e.test.ts 覆盖；这里验用户看得见的交互。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LlmSettings, ProviderPresetDescriptor } from '@/lib/local-api';

const getLlmSettings = vi.fn();
const setLlmSettings = vi.fn();
const getCompatFlags = vi.fn();
const getProviderPresets = vi.fn();
const resolveProviderPreset = vi.fn();
const getCatalogProviders = vi.fn();
const getLlmModels = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => true,
  getElectronBridge: () => undefined,
}));
vi.mock('@/lib/local-api', () => ({
  getLlmSettings: () => getLlmSettings(),
  setLlmSettings: (s: LlmSettings) => setLlmSettings(s),
  getCompatFlags: () => getCompatFlags(),
  getProviderPresets: () => getProviderPresets(),
  resolveProviderPreset: (baseUrl?: string, model?: string) =>
    resolveProviderPreset(baseUrl, model),
  getCatalogProviders: () => getCatalogProviders(),
  getLlmModels: (draft?: {
    baseUrl?: string;
    apiKey?: string;
    provider?: string;
    refresh?: boolean;
  }) => getLlmModels(draft),
}));

const { LocalLlmSettingsModal } = await import('./LocalLlmSettingsModal');

const DEEPSEEK: ProviderPresetDescriptor = {
  host: 'api.deepseek.com',
  modelPrefix: 'deepseek',
  temperature: 0.0,
  topP: null,
  maxTokens: null,
  reasoningEffort: null,
  extraBody: null,
};
const QWEN: ProviderPresetDescriptor = {
  host: null,
  modelPrefix: 'qwen3',
  temperature: 0.6,
  topP: 0.95,
  maxTokens: null,
  reasoningEffort: null,
  extraBody: { top_k: 20 },
};

const BASE_SETTINGS: LlmSettings = {
  provider: 'openai-compat',
  model: 'deepseek-chat',
  baseUrl: 'https://api.deepseek.com',
  apiKey: 'sk-test',
  maxTotalTokens: 60000,
};

function presetSection(): HTMLElement {
  return screen.getByText('厂商参数预制').closest('div')!.parentElement as HTMLElement;
}

async function renderOpen(settings: Partial<LlmSettings> = {}) {
  getLlmSettings.mockResolvedValue({ ...BASE_SETTINGS, ...settings });
  const utils = render(<LocalLlmSettingsModal open onClose={() => {}} />);
  // 等 reload 完成：预制区出现即数据就绪。
  await screen.findByText('厂商参数预制');
  return utils;
}

beforeEach(() => {
  vi.clearAllMocks();
  getCompatFlags.mockResolvedValue({ flags: [] });
  getProviderPresets.mockResolvedValue({ presets: [DEEPSEEK, QWEN] });
  resolveProviderPreset.mockResolvedValue({ preset: { temperature: 0.0 } });
  getCatalogProviders.mockResolvedValue({
    providers: [
      {
        id: 'deepseek',
        apiBaseUrl: 'https://api.deepseek.com',
        envVars: ['DEEPSEEK_API_KEY'],
        wireKind: 'openai_compat',
        models: ['deepseek-chat', 'deepseek-v4-flash'],
      },
      {
        id: 'anthropic',
        apiBaseUrl: 'https://api.anthropic.com',
        envVars: ['ANTHROPIC_API_KEY'],
        wireKind: 'anthropic',
        models: ['claude-sonnet-4-6', 'claude-opus-4-7'],
      },
    ],
  });
  getLlmModels.mockResolvedValue({ models: [], catalogStatus: 'offline' });
  setLlmSettings.mockImplementation(async (s: LlmSettings) => s);
});

afterEach(cleanup);

describe('LocalLlmSettingsModal — 遮罩关闭', () => {
  it('点在遮罩上关闭；面板内按下后在遮罩弹起不关', async () => {
    const onClose = vi.fn();
    getLlmSettings.mockResolvedValue(BASE_SETTINGS);
    render(<LocalLlmSettingsModal open onClose={onClose} />);
    const dialog = await screen.findByTestId('llm-settings-dialog');
    const panel = dialog.firstElementChild as HTMLElement;

    fireEvent.mouseDown(panel);
    fireEvent.mouseUp(dialog);
    fireEvent.click(dialog);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.mouseDown(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('LocalLlmSettingsModal — 厂商参数预制', () => {
  it('openai-compat 显示预制区；ollama 不显示', async () => {
    const { unmount } = await renderOpen();
    expect(screen.getByText('厂商参数预制')).toBeTruthy();
    expect(screen.queryByText('本地技能管理')).toBeNull();
    expect(screen.queryByText('MCP 服务')).toBeNull();
    unmount();

    getLlmSettings.mockResolvedValue({
      ...BASE_SETTINGS,
      provider: 'ollama',
      model: 'llama3.1:8b',
      baseUrl: 'http://127.0.0.1:11434',
    });
    render(<LocalLlmSettingsModal open onClose={() => {}} />);
    await screen.findByText('Temperature');
    expect(screen.queryByText('厂商参数预制')).toBeNull();
  });

  it('「自动」档预览命中参数，保存不写 presets（注册表自动匹配）', async () => {
    await renderOpen();
    // 防抖 300ms 后展示 resolve 结果。
    const preview = await screen.findByText(/命中预制：temperature 0/, undefined, {
      timeout: 3000,
    });
    expect(preview).toBeTruthy();
    expect(resolveProviderPreset).toHaveBeenCalledWith(
      'https://api.deepseek.com',
      'deepseek-chat',
    );

    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].presets).toBeUndefined();
  });

  it('「关闭」保存 {enabled:false}，预览说明不下发', async () => {
    await renderOpen();
    fireEvent.click(document.querySelector('[data-preset-mode="off"]')!);
    expect(screen.getByText('已关闭：不下发厂商预制参数。')).toBeTruthy();

    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].presets).toEqual({ enabled: false });
  });

  it('「指定预制」下拉选中注册表条目，保存其 override', async () => {
    await renderOpen();
    fireEvent.click(document.querySelector('[data-preset-mode="pinned"]')!);

    const select = presetSection().querySelector('select')!;
    expect(select).toBeTruthy();
    // 选中 qwen3 行（注册表第二个条目）。
    fireEvent.change(select, { target: { value: '1' } });
    expect(
      screen.getByText(/生效参数：temperature 0.6 · top_p 0.95 · top_k 20/),
    ).toBeTruthy();

    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].presets).toEqual({
      override: { temperature: 0.6, topP: 0.95, extraBody: { top_k: 20 } },
    });
  });

  it('已保存的钉死覆盖在注册表命中时回选对应行', async () => {
    await renderOpen({
      presets: { override: { temperature: 0.6, topP: 0.95, extraBody: { top_k: 20 } } },
    });
    // pinned 档直接展示下拉且选中 qwen3 行（下标 1）。
    const select = presetSection().querySelector('select')!;
    expect(select.value).toBe('1');
  });

  it('Temperature 默认自动（无滑杆、保存不下发），切手动出滑杆并持久化', async () => {
    await renderOpen();
    // 自动：提示文案在，滑杆不在；命中预览带入当前温度。
    await screen.findByText(/自动：命中厂商预制时用预制温度/, undefined, {
      timeout: 3000,
    });
    await screen.findByText(/（当前命中：0）/, undefined, { timeout: 3000 });
    expect(document.querySelector('input[type="range"]')).toBeNull();

    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].temperature).toBeUndefined();

    // 手动：滑杆出现，初值取命中预制的温度（0.0）。
    setLlmSettings.mockClear();
    fireEvent.click(document.querySelector('[data-temperature-mode="manual"]')!);
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement;
    expect(slider).toBeTruthy();
    expect(slider.value).toBe('0');

    fireEvent.change(slider, { target: { value: '0.5' } });
    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].temperature).toBe(0.5);
  });

  it('选择 Anthropic 自动填 URL 并隐藏 OpenAI 预制区', async () => {
    await renderOpen();
    fireEvent.change(screen.getByTestId('llm-vendor-select'), { target: { value: 'anthropic' } });
    const url = screen.getByPlaceholderText('https://api.anthropic.com') as HTMLInputElement;
    expect(url.value).toBe('https://api.anthropic.com');
    expect(screen.queryByText('厂商参数预制')).toBeNull();
    expect((screen.getByTestId('llm-model-input') as HTMLInputElement).value).toBe(
      'claude-sonnet-4-6',
    );
  });

  it('选择自定义后 URL 留空供手填', async () => {
    await renderOpen();
    fireEvent.change(screen.getByTestId('llm-vendor-select'), { target: { value: 'custom' } });
    const url = screen.getByPlaceholderText('https://your-gateway.example/v1') as HTMLInputElement;
    expect(url.value).toBe('');
  });

  it('保存时写入 vendorId 与对应的 sidecar provider', async () => {
    await renderOpen();
    fireEvent.change(screen.getByTestId('llm-vendor-select'), { target: { value: 'anthropic' } });
    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0]).toMatchObject({
      provider: 'anthropic',
      vendorId: 'anthropic',
      baseUrl: 'https://api.anthropic.com',
    });
  });

  it('模型下拉以网关实时目录为准，不混入服务商内置列表', async () => {
    getLlmModels.mockResolvedValue({
      models: [{ id: 'deepseek-chat' }, { id: 'deepseek-reasoner' }],
      catalogStatus: 'live',
    });
    await renderOpen();
    await waitFor(() =>
      expect(screen.getByText(/已从网关拉取 2 个模型/)).toBeTruthy(),
    );
    fireEvent.focus(screen.getByTestId('llm-model-input'));
    expect(screen.getByRole('option', { name: 'deepseek-reasoner' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'deepseek-v4-flash' })).toBeNull();
  });

  it('刷新按钮绕过缓存重新拉取网关目录', async () => {
    getLlmModels.mockResolvedValue({
      models: [{ id: 'deepseek-chat' }],
      catalogStatus: 'live',
    });
    await renderOpen();
    await waitFor(() =>
      expect(screen.getByText(/已从网关拉取 1 个模型/)).toBeTruthy(),
    );
    getLlmModels.mockClear();
    getLlmModels.mockResolvedValue({
      models: [{ id: 'deepseek-flash' }, { id: 'deepseek-v4-pro' }],
      catalogStatus: 'live',
    });
    fireEvent.click(screen.getByTestId('llm-model-refresh'));
    await waitFor(() => expect(getLlmModels).toHaveBeenCalled());
    expect(getLlmModels.mock.calls.at(-1)?.[0]).toMatchObject({ refresh: true });
    await waitFor(() =>
      expect(screen.getByText(/已从网关拉取 2 个模型/)).toBeTruthy(),
    );
  });

  it('测试按钮用当前凭证验证网关并回报结果', async () => {
    getLlmModels.mockResolvedValue({
      models: [{ id: 'deepseek-chat' }],
      catalogStatus: 'live',
    });
    await renderOpen();
    await waitFor(() => expect(screen.getByTestId('llm-key-test')).toBeTruthy());
    getLlmModels.mockClear();
    getLlmModels.mockResolvedValue({
      models: [{ id: 'deepseek-flash' }, { id: 'deepseek-v4-pro' }],
      catalogStatus: 'live',
    });
    fireEvent.click(screen.getByTestId('llm-key-test'));
    await waitFor(() => expect(getLlmModels).toHaveBeenCalled());
    expect(getLlmModels.mock.calls.at(-1)?.[0]).toMatchObject({ refresh: true });
    await waitFor(() =>
      expect(screen.getByText(/凭证可用，网关返回 2 个模型/)).toBeTruthy(),
    );
  });

  it('测试失败时展示网关错误', async () => {
    getLlmModels.mockResolvedValue({
      models: [],
      catalogStatus: 'offline',
      error: '401 unauthorized',
    });
    await renderOpen();
    await waitFor(() => expect(screen.getByTestId('llm-key-test')).toBeTruthy());
    fireEvent.click(screen.getByTestId('llm-key-test'));
    await waitFor(() =>
      expect(screen.getByText(/验证失败：401 unauthorized/)).toBeTruthy(),
    );
  });

  it('选中模型展示核实过的思考档位，未 join 的 id 只标未识别', async () => {
    getLlmSettings.mockResolvedValue({
      ...BASE_SETTINGS,
      model: 'deepseek-v4-pro',
    });
    getLlmModels.mockResolvedValue({
      models: [
        {
          id: 'deepseek-v4-pro',
          name: 'DeepSeek V4 Pro',
          window: 1_000_000,
          modalities: ['text'],
          reasoningLevels: ['high', 'max'],
          pricing: null,
          joinedFrom: 'deepseek/deepseek-v4-pro',
          capabilities: 'known',
        },
        {
          id: 'deepseek-flash',
          name: null,
          window: 131_072,
          modalities: ['text'],
          reasoningLevels: [],
          pricing: null,
          joinedFrom: null,
          capabilities: 'unknown',
        },
      ],
      catalogStatus: 'live',
    });
    await renderOpen({ model: 'deepseek-v4-pro' });
    const summary = await screen.findByTestId('llm-model-capabilities');
    expect(summary.textContent).toContain('思考（high / max）');
    expect(summary.textContent).toContain('1M');
    expect(summary.textContent).not.toContain('未识别');

    fireEvent.focus(screen.getByTestId('llm-model-input'));
    fireEvent.click(screen.getByRole('option', { name: 'deepseek-flash' }));
    await waitFor(() =>
      expect(screen.getByTestId('llm-model-capabilities').textContent).toContain('未识别'),
    );
    expect(screen.getByTestId('llm-model-capabilities').textContent).not.toContain('131K');
  });
});
