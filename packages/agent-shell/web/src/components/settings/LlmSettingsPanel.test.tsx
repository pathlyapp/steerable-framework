/**
 * LlmSettingsPanel — 本地模型设置面板的渲染与交互：
 *   - 非桌面环境提示「需要在桌面客户端中打开」，不发起任何配置请求；
 *   - 挂载自取配置：加载态 → 表单回填（服务商 / URL / Key / 模型 / 预算 / 超时）；
 *   - 切换服务商自动填 URL 与默认模型；ollama 隐藏 API Key 区；
 *   - 测试凭证 / 刷新模型目录直连网关（refresh 绕过缓存）；
 *   - compat 旗标三态（自动/开/关、枚举、列表）只下发非自动覆盖；
 *   - 保存走 local-api setLlmSettings：成功回调 onSaved、失败展示错误；
 *     showFooterSave=false 时经 ref.save() 触发同样的保存。
 * 厂商参数预制的完整交互（钉死下拉、命中预览、温度联动）由
 * components/LocalLlmSettingsModal.test.tsx 覆盖，这里只验面板级接线。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  CompatFlagDescriptor,
  LlmSettings,
  ProviderPresetDescriptor,
} from '@/lib/local-api';
import type { LlmSettingsPanelHandle } from './LlmSettingsPanel';

const getLlmSettings = vi.fn();
const setLlmSettings = vi.fn();
const getCompatFlags = vi.fn();
const getProviderPresets = vi.fn();
const resolveProviderPreset = vi.fn();
const getCatalogProviders = vi.fn();
const getLlmModels = vi.fn();
const electronState = { active: true };

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => electronState.active,
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

const { LlmSettingsPanel } = await import('./LlmSettingsPanel');

// 通用占位服务商（框架目录由 sidecar 下发，这里用虚构条目避免产品耦合）。
const ACME = {
  id: 'acme',
  apiBaseUrl: 'https://api.acme.example/v1',
  envVars: ['ACME_API_KEY'],
  wireKind: 'openai_compat',
  models: ['acme-chat', 'acme-pro'],
};
const MESSAGES_API = {
  id: 'messages-api',
  apiBaseUrl: 'https://messages.example',
  envVars: ['MESSAGES_API_KEY'],
  wireKind: 'anthropic',
  models: ['msg-large', 'msg-small'],
};

const BASE_SETTINGS: LlmSettings = {
  provider: 'openai-compat',
  vendorId: 'acme',
  model: 'acme-chat',
  baseUrl: 'https://api.acme.example/v1',
  apiKey: 'sk-test',
  maxTotalTokens: 60000,
};

const COMPAT_FLAGS: CompatFlagDescriptor[] = [
  {
    key: 'supportsTemperature',
    field: 'supports_temperature',
    kind: 'bool',
    default: null,
    description: '网关是否接受 temperature 字段。',
  },
  {
    key: 'maxTokensField',
    field: 'max_tokens_field',
    kind: 'enum:max_tokens,max_completion_tokens',
    default: null,
    description: '最大 token 字段名。',
  },
  {
    key: 'reasoningDeltaFields',
    field: 'reasoning_delta_fields',
    kind: 'string-list',
    default: null,
    description: '推理增量可能出现的字段。',
  },
];

const ACME_PRESET: ProviderPresetDescriptor = {
  host: 'api.acme.example',
  modelPrefix: 'acme',
  temperature: 0.2,
  topP: null,
  maxTokens: null,
  reasoningEffort: null,
  extraBody: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  electronState.active = true;
  getCompatFlags.mockResolvedValue({ flags: COMPAT_FLAGS });
  getProviderPresets.mockResolvedValue({ presets: [ACME_PRESET] });
  resolveProviderPreset.mockResolvedValue({ preset: null });
  getCatalogProviders.mockResolvedValue({ providers: [ACME, MESSAGES_API] });
  getLlmModels.mockResolvedValue({ models: [], catalogStatus: 'offline' });
  setLlmSettings.mockImplementation(async (s: LlmSettings) => s);
});

afterEach(cleanup);

interface PanelProps {
  onSaved?: (settings: LlmSettings) => void;
  onSaveUiChange?: (ui: { saving: boolean; savedOk: boolean; loading: boolean }) => void;
  showFooterSave?: boolean;
  ref?: React.Ref<LlmSettingsPanelHandle>;
}

/** 渲染并等 reload 完成（服务商下拉出现即数据就绪）。 */
async function renderPanel(settings: Partial<LlmSettings> = {}, props: PanelProps = {}) {
  getLlmSettings.mockResolvedValue({ ...BASE_SETTINGS, ...settings });
  const utils = render(<LlmSettingsPanel {...props} />);
  await screen.findByTestId('llm-vendor-select');
  return utils;
}

/** 等挂载后的模型目录防抖拉取（400ms）落定，避免干扰后续调用断言。 */
async function settleCatalog() {
  await screen.findByText(/网关目录不可用/, undefined, { timeout: 3000 });
}

describe('LlmSettingsPanel — 环境与加载', () => {
  it('非桌面环境提示需要在客户端打开，且不请求配置', async () => {
    electronState.active = false;
    render(<LlmSettingsPanel />);
    expect(await screen.findByText('需要在桌面客户端中打开')).toBeTruthy();
    expect(getLlmSettings).not.toHaveBeenCalled();
  });

  it('加载期间显示加载态并禁用保存，完成后渲染表单', async () => {
    let resolveSettings: (s: LlmSettings) => void = () => {};
    getLlmSettings.mockReturnValue(
      new Promise((resolve) => {
        resolveSettings = resolve;
      }),
    );
    render(<LlmSettingsPanel />);
    expect(await screen.findByText('加载当前配置...')).toBeTruthy();
    expect((screen.getByTestId('llm-settings-save') as HTMLButtonElement).disabled).toBe(true);

    resolveSettings({ ...BASE_SETTINGS });
    expect(await screen.findByTestId('llm-vendor-select')).toBeTruthy();
    expect((screen.getByTestId('llm-settings-save') as HTMLButtonElement).disabled).toBe(false);
  });

  it('读取配置失败时展示错误', async () => {
    getLlmSettings.mockRejectedValue(new Error('存储损坏'));
    render(<LlmSettingsPanel />);
    expect(await screen.findByText('存储损坏')).toBeTruthy();
  });

  it('sidecar 未就绪时表单仍可用，旗标区与预制下拉给出提示', async () => {
    getCompatFlags.mockRejectedValue(new Error('sidecar down'));
    getProviderPresets.mockRejectedValue(new Error('sidecar down'));
    getCatalogProviders.mockRejectedValue(new Error('sidecar down'));
    await renderPanel();
    expect(screen.getByText(/旗标词汇表由 sidecar 提供/)).toBeTruthy();

    fireEvent.click(document.querySelector('[data-preset-mode="pinned"]')!);
    expect(screen.getByText(/预制注册表由 sidecar 提供/)).toBeTruthy();

    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
  });
});

describe('LlmSettingsPanel — 表单回填与渲染', () => {
  it('回填已保存的服务商、URL、Key、模型、预算与超时', async () => {
    await renderPanel({ maxTotalTokens: 8000, execTimeoutSeconds: 120 });
    expect((screen.getByTestId('llm-vendor-select') as HTMLSelectElement).value).toBe('acme');
    expect(screen.getByDisplayValue('https://api.acme.example/v1')).toBeTruthy();
    expect(screen.getByDisplayValue('sk-test')).toBeTruthy();
    expect((screen.getByTestId('llm-model-input') as HTMLInputElement).value).toBe('acme-chat');
    expect(screen.getByDisplayValue('8000')).toBeTruthy();
    expect(screen.getByDisplayValue('120')).toBeTruthy();
  });

  it('ollama 服务商不渲染 API Key 区', async () => {
    await renderPanel({
      provider: 'ollama',
      vendorId: 'ollama',
      model: 'llama3.1:8b',
      baseUrl: 'http://127.0.0.1:11434',
      apiKey: '',
    });
    expect(screen.queryByText('API Key')).toBeNull();
    expect(screen.queryByTestId('llm-key-test')).toBeNull();
  });

  it('未填写 API Key 时显示创建密钥的引导提示', async () => {
    await renderPanel({ apiKey: '' });
    expect(screen.getByText(/尚未配置 API Key/)).toBeTruthy();
  });

  it('切换服务商后自动填入默认 URL 与目录首个模型，并隐藏 OpenAI 专属区块', async () => {
    await renderPanel();
    fireEvent.change(screen.getByTestId('llm-vendor-select'), {
      target: { value: 'messages-api' },
    });
    const url = screen.getByPlaceholderText('https://messages.example') as HTMLInputElement;
    expect(url.value).toBe('https://messages.example');
    expect((screen.getByTestId('llm-model-input') as HTMLInputElement).value).toBe('msg-large');
    // anthropic 原生协议：不显示 compat 旗标区与厂商预制区
    expect(screen.queryByText('高级兼容旗标（可选）')).toBeNull();
    expect(screen.queryByText('厂商参数预制')).toBeNull();
  });
});

describe('LlmSettingsPanel — 网关目录与凭证', () => {
  it('「测试」用当前 URL 与 Key 拉取网关目录并回报模型数', async () => {
    getLlmModels.mockResolvedValue({
      models: [{ id: 'acme-chat' }, { id: 'acme-pro' }],
      catalogStatus: 'live',
    });
    await renderPanel();
    await screen.findByText(/已从网关拉取 2 个模型/, undefined, { timeout: 3000 });

    getLlmModels.mockClear();
    fireEvent.click(screen.getByTestId('llm-key-test'));
    await screen.findByText(/凭证可用，网关返回 2 个模型/);
    expect(getLlmModels.mock.calls.at(-1)?.[0]).toMatchObject({
      baseUrl: 'https://api.acme.example/v1',
      apiKey: 'sk-test',
      refresh: true,
    });
  });

  it('凭证验证失败时展示网关返回的错误', async () => {
    await renderPanel();
    await settleCatalog();
    getLlmModels.mockResolvedValue({
      models: [],
      catalogStatus: 'offline',
      error: '401 unauthorized',
    });
    fireEvent.click(screen.getByTestId('llm-key-test'));
    expect(await screen.findByText(/验证失败：401 unauthorized/)).toBeTruthy();
  });

  it('刷新按钮绕过缓存重新拉取模型目录', async () => {
    getLlmModels.mockResolvedValue({
      models: [{ id: 'acme-chat' }],
      catalogStatus: 'live',
    });
    await renderPanel();
    await screen.findByText(/已从网关拉取 1 个模型/, undefined, { timeout: 3000 });

    getLlmModels.mockClear();
    getLlmModels.mockResolvedValue({
      models: [{ id: 'acme-chat' }, { id: 'acme-pro' }],
      catalogStatus: 'live',
    });
    fireEvent.click(screen.getByTestId('llm-model-refresh'));
    await screen.findByText(/已从网关拉取 2 个模型/);
    expect(getLlmModels.mock.calls.at(-1)?.[0]).toMatchObject({ refresh: true });
  });

  it('修改 Base URL 后按新地址防抖拉取模型目录', async () => {
    await renderPanel();
    await settleCatalog();
    getLlmModels.mockClear();
    fireEvent.change(screen.getByDisplayValue('https://api.acme.example/v1'), {
      target: { value: 'https://relay.example/v1' },
    });
    await waitFor(() => expect(getLlmModels).toHaveBeenCalled(), { timeout: 3000 });
    expect(getLlmModels.mock.calls.at(-1)?.[0]).toMatchObject({
      baseUrl: 'https://relay.example/v1',
    });
  });
});

describe('LlmSettingsPanel — compat 旗标', () => {
  it('按词汇表渲染 bool / 枚举 / 列表三种旗标，保存时只下发非自动覆盖', async () => {
    await renderPanel();
    const boolRow = document.querySelector('[data-compat-flag="supportsTemperature"]')!;
    fireEvent.click(within(boolRow as HTMLElement).getByText('开'));

    const enumRow = document.querySelector('[data-compat-flag="maxTokensField"]')!;
    fireEvent.change(within(enumRow as HTMLElement).getByRole('combobox'), {
      target: { value: 'max_completion_tokens' },
    });

    const listRow = document.querySelector('[data-compat-flag="reasoningDeltaFields"]')!;
    fireEvent.change(within(listRow as HTMLElement).getByRole('textbox'), {
      target: { value: 'reasoning, delta' },
    });

    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].compat).toEqual({
      supportsTemperature: true,
      maxTokensField: 'max_completion_tokens',
      reasoningDeltaFields: ['reasoning', 'delta'],
    });
  });

  it('旗标全部自动时保存不写 compat', async () => {
    await renderPanel();
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].compat).toBeUndefined();
  });

  it('已保存的覆盖回填为对应档位', async () => {
    await renderPanel({
      compat: { supportsTemperature: false, maxTokensField: 'max_completion_tokens' },
    });
    const boolRow = document.querySelector('[data-compat-flag="supportsTemperature"]')!;
    const offButton = within(boolRow as HTMLElement).getByText('关');
    expect(offButton.className).toContain('bg-agent-foreground');
    const enumRow = document.querySelector('[data-compat-flag="maxTokensField"]')!;
    expect(
      (within(enumRow as HTMLElement).getByRole('combobox') as HTMLSelectElement).value,
    ).toBe('max_completion_tokens');
  });
});

describe('LlmSettingsPanel — 保存流程', () => {
  it('保存成功：下发完整载荷、回调 onSaved 并上报保存态', async () => {
    const onSaved = vi.fn();
    const onSaveUiChange = vi.fn();
    await renderPanel({}, { onSaved, onSaveUiChange });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onSaved.mock.calls[0][0]).toMatchObject({
      provider: 'openai-compat',
      vendorId: 'acme',
      model: 'acme-chat',
      baseUrl: 'https://api.acme.example/v1',
      apiKey: 'sk-test',
    });
    // 加载期间上报过 loading:true；保存完成后 savedOk:true。
    expect(onSaveUiChange.mock.calls.some(([ui]) => ui.loading === true)).toBe(true);
    await waitFor(() => {
      expect(onSaveUiChange.mock.calls.at(-1)?.[0]).toMatchObject({
        saving: false,
        savedOk: true,
        loading: false,
      });
    });
  });

  it('保存失败时展示错误且不回调 onSaved', async () => {
    setLlmSettings.mockRejectedValue(new Error('磁盘只读'));
    const onSaved = vi.fn();
    await renderPanel({}, { onSaved });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    expect(await screen.findByText('磁盘只读')).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('保存时 trim URL 与 Key，空白 Key 不下发', async () => {
    await renderPanel();
    fireEvent.change(screen.getByDisplayValue('https://api.acme.example/v1'), {
      target: { value: '  https://api.acme.example/v2  ' },
    });
    fireEvent.change(screen.getByDisplayValue('sk-test'), { target: { value: '   ' } });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].baseUrl).toBe('https://api.acme.example/v2');
    expect(setLlmSettings.mock.calls[0][0].apiKey).toBeUndefined();
  });

  it('本地命令超时留空或非法时不下发，填正整数时随保存下发', async () => {
    await renderPanel();
    const input = screen.getByPlaceholderText('留空 = 默认（后台 30s / 终端 60s）');
    fireEvent.change(input, { target: { value: 'abc' } });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].execTimeoutSeconds).toBeUndefined();

    setLlmSettings.mockClear();
    fireEvent.change(input, { target: { value: '90' } });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].execTimeoutSeconds).toBe(90);
  });

  it('Token 预算与手填模型 id 随保存下发', async () => {
    await renderPanel();
    fireEvent.change(screen.getByDisplayValue('60000'), { target: { value: '12000' } });
    fireEvent.change(screen.getByTestId('llm-model-input'), { target: { value: 'acme-pro' } });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].maxTotalTokens).toBe(12000);
    expect(setLlmSettings.mock.calls[0][0].model).toBe('acme-pro');
  });

  it('showFooterSave=false 时不渲染底部按钮，可经 ref.save() 触发保存', async () => {
    const ref = createRef<LlmSettingsPanelHandle>();
    await renderPanel({}, { showFooterSave: false, ref });
    expect(screen.queryByTestId('llm-settings-save')).toBeNull();
    await act(async () => {
      await ref.current!.save();
    });
    expect(setLlmSettings).toHaveBeenCalled();
  });
});

describe('LlmSettingsPanel — 温度与预制（面板级接线）', () => {
  it('Temperature 缺省自动（无滑杆、保存不下发），切手动后滑杆值随保存下发', async () => {
    await renderPanel();
    expect(document.querySelector('input[type="range"]')).toBeNull();
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].temperature).toBeUndefined();

    setLlmSettings.mockClear();
    fireEvent.click(document.querySelector('[data-temperature-mode="manual"]')!);
    const slider = document.querySelector('input[type="range"]') as HTMLInputElement;
    expect(slider).toBeTruthy();
    fireEvent.change(slider, { target: { value: '0.5' } });
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].temperature).toBe(0.5);
  });

  it('「自动」档命中预制时预览生效参数，保存不写 presets', async () => {
    resolveProviderPreset.mockResolvedValue({ preset: { temperature: 0.2 } });
    await renderPanel();
    expect(
      await screen.findByText(/命中预制：temperature 0.2/, undefined, { timeout: 3000 }),
    ).toBeTruthy();
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].presets).toBeUndefined();
  });

  it('预制「关闭」保存 {enabled:false}', async () => {
    await renderPanel();
    fireEvent.click(document.querySelector('[data-preset-mode="off"]')!);
    expect(screen.getByText('已关闭：不下发厂商预制参数。')).toBeTruthy();
    fireEvent.click(screen.getByTestId('llm-settings-save'));
    await waitFor(() => expect(setLlmSettings).toHaveBeenCalled());
    expect(setLlmSettings.mock.calls[0][0].presets).toEqual({ enabled: false });
  });
});
