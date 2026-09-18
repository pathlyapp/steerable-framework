/**
 * LocalStore 设置项（settings_kv）集成测试：真实 SQLite。
 *
 * 覆盖：LLM / 遥测 / 网络搜索三类设置的持久化往返、合并默认值、
 * 坏行容错（存了坏 JSON 读为 null），以及新库构造时 LLM 出厂默认的
 * 种子写出。纯合并逻辑本身由 llm-settings / telemetry-settings /
 * web-search-settings 的专属测试覆盖，这里聚焦「落库 ↔ 读回」。
 */
import { afterEach, describe, expect, it } from 'vitest';

import { DEFAULT_LLM_SETTINGS } from '../../src/storage/llm-settings.js';
import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

const { LocalStore } = await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
});

describe('LocalStore / LLM 设置', () => {
  it('新库构造后种子写出厂默认（DeepSeek 预设，无内置 key）', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.getLlmSettings()).toEqual(DEFAULT_LLM_SETTINGS);
    expect(store.getLlmSettings()?.apiKey).toBeUndefined();
  });

  it('setLlmSettings 往返：合并缺省字段后落库，读回一致', () => {
    const { store } = createTestStore(LocalStore);
    const merged = store.setLlmSettings({
      provider: 'ollama',
      model: 'llama3.1:8b',
      baseUrl: 'http://127.0.0.1:11434',
    });
    expect(merged.provider).toBe('ollama');
    // 返回值与读回值一致（set 返回的就是合并后落库的那份）。
    expect(store.getLlmSettings()).toEqual(merged);
  });

  it('覆盖写：后一次 set 整体替换（ON CONFLICT 更新）', () => {
    const { store } = createTestStore(LocalStore);
    store.setLlmSettings({ provider: 'openai-compat', model: 'm1', apiKey: 'sk-1' });
    const second = store.setLlmSettings({ provider: 'openai-compat', model: 'm2' });
    expect(store.getLlmSettings()?.model).toBe('m2');
    expect(second.model).toBe('m2');
  });

  it('settings_kv 里存了坏 JSON 时读为 null', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(`UPDATE settings_kv SET value = ? WHERE key = 'llm_settings'`)
      .run('not-json');
    expect(store.getLlmSettings()).toBeNull();
  });
});

describe('LocalStore / 遥测设置', () => {
  it('从未配置过时返回 null（调用方按「关」处理）', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.getTelemetrySettings()).toBeNull();
  });

  it('set/get 往返：合并默认 privacyMode 与 serviceName', () => {
    const { store } = createTestStore(LocalStore);
    const merged = store.setTelemetrySettings({ endpoint: 'http://127.0.0.1:4318/v1/traces' });
    expect(merged).toEqual({
      endpoint: 'http://127.0.0.1:4318/v1/traces',
      privacyMode: 'metadata',
      serviceName: 'steerable-agent-desktop',
    });
    expect(store.getTelemetrySettings()).toEqual(merged);
  });

  it('非法 endpoint 归一为未配置（绝不让坏地址把遥测打开）', () => {
    const { store } = createTestStore(LocalStore);
    const merged = store.setTelemetrySettings({ endpoint: 'file:///etc/passwd' });
    expect(merged.endpoint).toBeUndefined();
    expect(store.getTelemetrySettings()?.endpoint).toBeUndefined();
  });

  it('坏 JSON 读为 null', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(`INSERT INTO settings_kv (key, value) VALUES ('telemetry_settings', ?)`)
      .run('{broken');
    expect(store.getTelemetrySettings()).toBeNull();
  });
});

describe('LocalStore / 网络搜索设置', () => {
  it('从未配置过时返回 null', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.getWebSearchSettings()).toBeNull();
  });

  it('set/get 往返：apiKey trim，空 key 归一为未配置', () => {
    const { store } = createTestStore(LocalStore);
    const merged = store.setWebSearchSettings({ provider: 'ddg', apiKey: '  key-1  ' });
    expect(merged).toEqual({ provider: 'ddg', apiKey: 'key-1' });
    expect(store.getWebSearchSettings()).toEqual(merged);

    const cleared = store.setWebSearchSettings({ provider: 'tavily', apiKey: '   ' });
    expect(cleared.apiKey).toBeUndefined();
  });

  it('未知 provider 归一为 tavily；坏 JSON 读为 null', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(
        `INSERT INTO settings_kv (key, value) VALUES ('web_search_settings', ?)`,
      )
      .run(JSON.stringify({ provider: 'bogus', apiKey: 'k' }));
    expect(store.getWebSearchSettings()?.provider).toBe('tavily');

    store
      .getPackDb()
      .prepare(`UPDATE settings_kv SET value = ? WHERE key = 'web_search_settings'`)
      .run('not-json');
    expect(store.getWebSearchSettings()).toBeNull();
  });
});
