/**
 * insights.trackBehavior：fire-and-forget 的行为事件入队。
 * 锁定三条契约：桥缺失时静默返回（浏览器演示态不产生遥测）、
 * 请求形状逐字锁定（端点 / 方法 / body 键）、后端失败绝不抛进 UI。
 * 桥走真实的 window.electron 路径，不 mock 模块。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { trackBehavior } from './insights';

type RequestInput = { method: string; path: string; body?: unknown };

function installBridge() {
  const request = vi.fn<(input: RequestInput) => Promise<unknown>>();
  (window as { electron?: unknown }).electron = { localBackend: { request } };
  return request;
}

afterEach(() => {
  delete (window as { electron?: unknown }).electron;
});

describe('trackBehavior', () => {
  it('桥缺失时静默返回，不抛错', () => {
    expect(() => trackBehavior('chat_opened')).not.toThrow();
  });

  it('桥存在时 POST 到事件端点，body 带事件名与属性', () => {
    const request = installBridge();
    request.mockResolvedValue({});
    trackBehavior('chat_opened', { source: 'sidebar' });
    expect(request).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/v2/insights/events',
      body: { eventName: 'chat_opened', properties: { source: 'sidebar' } },
    });
  });

  it('属性缺省为空对象', () => {
    const request = installBridge();
    request.mockResolvedValue({});
    trackBehavior('app_launch');
    expect(request).toHaveBeenCalledWith({
      method: 'POST',
      path: '/api/v2/insights/events',
      body: { eventName: 'app_launch', properties: {} },
    });
  });

  it('后端拒绝时吞掉错误，不抛进 UI', async () => {
    const request = installBridge();
    request.mockRejectedValue(new Error('backend down'));
    expect(() => trackBehavior('chat_opened')).not.toThrow();
    // 让内部 promise 链跑完，确认没有未处理拒绝冒出来。
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
});
