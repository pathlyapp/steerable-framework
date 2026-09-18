/**
 * insights 上报管线（insights/flush.ts）行为测试。
 *
 * 钉住的契约：
 *  - 测试环境（VITEST）/显式禁用 → 完全不上报，outbox 留本地；
 *  - 无端点（中性 shell 未注入遥测配置）→ 不上报；
 *  - 按 kind 分发到 events/turns/profile 路径，带 installId/flavor/appVersion
 *    与 clientEventId；成功标 uploaded、失败标 error，计数正确；
 *  - bundle 导出成功后批量标 uploaded；
 *  - 网络错误/超时/非 2xx 都吞掉返回 false（上报永远不能弄断产品）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  ensureInsightsSettings: vi.fn(),
  listInsightOutbox: vi.fn(),
  exportInsightsBundle: vi.fn(),
  markInsightUploaded: vi.fn(),
  markInsightUploadError: vi.fn(),
  rowsEligibleForAutoUpload: vi.fn(),
  resolveInsightsApiBase: vi.fn(),
}));

vi.mock('../src/storage/insights-settings.js', () => ({
  rowsEligibleForAutoUpload: mocks.rowsEligibleForAutoUpload,
  resolveInsightsApiBase: mocks.resolveInsightsApiBase,
}));

import {
  buildInsightsExportPayload,
  flushInsightsOutbox,
  insightsNetworkDisabled,
  postInsightsJson,
  uploadInsightsBundle,
} from '../src/insights/flush.js';

const ENV_KEYS = ['VITEST', 'DEEPPATH_INSIGHTS_DISABLE_UPLOAD', 'DEEPPATH_INSIGHTS_API_BASE', 'DEEPPATH_INSIGHTS_INGEST_TOKEN'];
let savedEnv: Record<string, string | undefined> = {};
const fetchMock = vi.fn();
const store = {
  ensureInsightsSettings: mocks.ensureInsightsSettings,
  listInsightOutbox: mocks.listInsightOutbox,
  exportInsightsBundle: mocks.exportInsightsBundle,
  markInsightUploaded: mocks.markInsightUploaded,
  markInsightUploadError: mocks.markInsightUploadError,
} as never;

beforeEach(() => {
  vi.clearAllMocks();
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  // flush 在 VITEST 下默认禁网——这里测的就是上报路径本身，显式打开。
  delete process.env.VITEST;
  delete process.env.DEEPPATH_INSIGHTS_DISABLE_UPLOAD;
  delete process.env.DEEPPATH_INSIGHTS_API_BASE;
  delete process.env.DEEPPATH_INSIGHTS_INGEST_TOKEN;
  vi.stubGlobal('fetch', fetchMock);
  mocks.ensureInsightsSettings.mockReturnValue({ installId: 'inst-1' });
  mocks.resolveInsightsApiBase.mockReturnValue('https://insights.example');
});

afterEach(() => {
  vi.unstubAllGlobals();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
});

describe('insightsNetworkDisabled', () => {
  it('VITEST 环境下禁网；显式开关禁网；都不在时放行', () => {
    expect(insightsNetworkDisabled()).toBe(false);
    process.env.VITEST = '1';
    expect(insightsNetworkDisabled()).toBe(true);
    delete process.env.VITEST;
    process.env.DEEPPATH_INSIGHTS_DISABLE_UPLOAD = '1';
    expect(insightsNetworkDisabled()).toBe(true);
  });
});

describe('flushInsightsOutbox', () => {
  it('禁网时全零返回，store 不被触碰', async () => {
    process.env.VITEST = '1';
    expect(await flushInsightsOutbox(store)).toEqual({ uploaded: 0, skipped: 0, failed: 0 });
    expect(mocks.listInsightOutbox).not.toHaveBeenCalled();
  });

  it('无端点（未注入遥测配置）→ 不上报，outbox 留本地', async () => {
    mocks.resolveInsightsApiBase.mockReturnValue(undefined);
    mocks.listInsightOutbox.mockReturnValue([{ id: 'r1', kind: 'event', payload: {} }]);
    mocks.rowsEligibleForAutoUpload.mockReturnValue({ upload: [{ id: 'r1', kind: 'event', payload: {} }], skipped: 0 });
    expect(await flushInsightsOutbox(store)).toEqual({ uploaded: 0, skipped: 0, failed: 0 });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mocks.markInsightUploaded).not.toHaveBeenCalled();
  });

  it('按 kind 分发到对应路径，带 meta 与 clientEventId；成功标 uploaded', async () => {
    const rows = [
      { id: 'e1', kind: 'event', payload: { name: 'login' } },
      { id: 't1', kind: 'turn', payload: { turns: 3 } },
      { id: 'p1', kind: 'profile', payload: { os: 'mac' } },
    ];
    mocks.listInsightOutbox.mockReturnValue(rows);
    mocks.rowsEligibleForAutoUpload.mockReturnValue({ upload: rows, skipped: 2 });
    fetchMock.mockResolvedValue({ ok: true });

    const result = await flushInsightsOutbox(store);
    expect(result).toEqual({ uploaded: 3, skipped: 2, failed: 0 });

    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toEqual([
      'https://insights.example/api/v2/agent-insights/events',
      'https://insights.example/api/v2/agent-insights/turns',
      'https://insights.example/api/v2/agent-insights/profile',
    ]);
    const firstBody = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(firstBody).toMatchObject({
      installId: 'inst-1',
      name: 'login',
      clientEventId: 'e1',
    });
    expect(typeof firstBody.flavor).toBe('string');
    expect(typeof firstBody.appVersion).toBe('string');
    for (const row of rows) {
      expect(mocks.markInsightUploaded).toHaveBeenCalledWith(row.id);
    }
  });

  it('部分失败：失败的标 error 并计数，成功的照常标 uploaded', async () => {
    const rows = [
      { id: 'ok1', kind: 'event', payload: {} },
      { id: 'bad1', kind: 'event', payload: {} },
    ];
    mocks.listInsightOutbox.mockReturnValue(rows);
    mocks.rowsEligibleForAutoUpload.mockReturnValue({ upload: rows, skipped: 0 });
    fetchMock.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce({ ok: false, status: 500 });

    expect(await flushInsightsOutbox(store)).toEqual({ uploaded: 1, skipped: 0, failed: 1 });
    expect(mocks.markInsightUploaded).toHaveBeenCalledWith('ok1');
    expect(mocks.markInsightUploadError).toHaveBeenCalledWith('bad1', 'upload_failed');
  });

  it('fetch 抛错（网络断）按失败计数，不抛出', async () => {
    const rows = [{ id: 'x1', kind: 'turn', payload: {} }];
    mocks.listInsightOutbox.mockReturnValue(rows);
    mocks.rowsEligibleForAutoUpload.mockReturnValue({ upload: rows, skipped: 0 });
    fetchMock.mockRejectedValue(new Error('socket hang up'));
    expect(await flushInsightsOutbox(store)).toEqual({ uploaded: 0, skipped: 0, failed: 1 });
  });
});

describe('postInsightsJson', () => {
  it('2xx → true；非 2xx → false；异常 → false', async () => {
    fetchMock.mockResolvedValueOnce({ ok: true });
    expect(await postInsightsJson('https://x.example/', {})).toBe(true);
    fetchMock.mockResolvedValueOnce({ ok: false });
    expect(await postInsightsJson('https://x.example/', {})).toBe(false);
    fetchMock.mockRejectedValueOnce(new Error('dns'));
    expect(await postInsightsJson('https://x.example/', {})).toBe(false);
  });

  it('ingest token 存在时带 X-DeepPath-Insights-Key 头（线协议常量）', async () => {
    process.env.DEEPPATH_INSIGHTS_INGEST_TOKEN = 'tok-1';
    fetchMock.mockResolvedValue({ ok: true });
    await postInsightsJson('https://x.example/', { a: 1 });
    expect(fetchMock.mock.calls[0][1].headers['X-DeepPath-Insights-Key']).toBe('tok-1');
  });

  it('无 token 时不带该头', async () => {
    fetchMock.mockResolvedValue({ ok: true });
    await postInsightsJson('https://x.example/', {});
    expect(fetchMock.mock.calls[0][1].headers['X-DeepPath-Insights-Key']).toBeUndefined();
  });
});

describe('uploadInsightsBundle / buildInsightsExportPayload', () => {
  it('payload = 导出 bundle + client meta', async () => {
    mocks.exportInsightsBundle.mockReturnValue({ schema: 'test/v1', records: [] });
    const payload = await buildInsightsExportPayload(store);
    expect(payload).toMatchObject({ schema: 'test/v1', records: [], installId: 'inst-1' });
    expect(typeof payload.flavor).toBe('string');
    expect(typeof payload.appVersion).toBe('string');
  });

  it('bundle 上传成功 → 所有 records 标 uploaded；失败 → 不标', async () => {
    mocks.exportInsightsBundle.mockReturnValue({
      records: [{ id: 'r1' }, { id: 'r2' }],
    });
    fetchMock.mockResolvedValueOnce({ ok: true });
    expect(await uploadInsightsBundle(store)).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toBe('https://insights.example/api/v2/agent-insights/bundle');
    expect(mocks.markInsightUploaded).toHaveBeenCalledWith('r1');
    expect(mocks.markInsightUploaded).toHaveBeenCalledWith('r2');

    vi.clearAllMocks();
    fetchMock.mockResolvedValueOnce({ ok: false });
    expect(await uploadInsightsBundle(store)).toBe(false);
    expect(mocks.markInsightUploaded).not.toHaveBeenCalled();
  });

  it('禁网/无端点 → false 且不发请求', async () => {
    process.env.VITEST = '1';
    expect(await uploadInsightsBundle(store)).toBe(false);
    delete process.env.VITEST;
    mocks.resolveInsightsApiBase.mockReturnValue(undefined);
    mocks.exportInsightsBundle.mockReturnValue({ records: [] });
    expect(await uploadInsightsBundle(store)).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
