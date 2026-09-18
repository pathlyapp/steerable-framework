/**
 * electron-bridge 的桥选择逻辑：window.electron（Electron preload）优先，
 * 否则 window.__DEEPPATH_BS__（BS 服务器托管）落到 HTTP 实现，
 * 两者皆无则是纯浏览器演示态（null）。SPA 必须在三种形态下都不崩。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { getElectronBridge, isElectron } from './electron-bridge';

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
  delete (window as { __DEEPPATH_BS__?: unknown }).__DEEPPATH_BS__;
});

describe('getElectronBridge / isElectron', () => {
  it('两种桥都不存在时返回 null（纯浏览器演示态）', () => {
    expect(getElectronBridge()).toBeNull();
    expect(isElectron()).toBe(false);
  });

  it('window.electron 存在时原样返回', () => {
    const fake = { runtime: 'local', platform: 'darwin' };
    (window as { electron?: unknown }).electron = fake;
    expect(getElectronBridge()).toBe(fake);
    expect(isElectron()).toBe(true);
  });

  it('仅 __DEEPPATH_BS__ 时落到 HTTP 桥，平台取自注入的引导信息', () => {
    (window as { __DEEPPATH_BS__?: unknown }).__DEEPPATH_BS__ = {
      platform: 'linux',
      flavor: 'generic',
      brandName: 'Test',
    };
    const bridge = getElectronBridge();
    expect(bridge).not.toBeNull();
    expect(bridge!.runtime).toBe('local');
    expect(bridge!.platform).toBe('linux');
    // HTTP 桥的传输面齐全（fetch 实现，无需 Electron）。
    expect(typeof bridge!.localBackend.request).toBe('function');
    expect(isElectron()).toBe(true);
    // 单例：再次取桥是同一个对象。
    expect(getElectronBridge()).toBe(bridge);
  });

  it('window.electron 优先于 __DEEPPATH_BS__', () => {
    const fake = { runtime: 'local', platform: 'darwin' };
    (window as { electron?: unknown }).electron = fake;
    (window as { __DEEPPATH_BS__?: unknown }).__DEEPPATH_BS__ = {
      platform: 'linux',
      flavor: 'generic',
      brandName: 'Test',
    };
    expect(getElectronBridge()).toBe(fake);
  });
});
