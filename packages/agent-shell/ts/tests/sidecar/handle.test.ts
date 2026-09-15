import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  getSidecarSupervisor,
  setSidecarSupervisor,
  setSidecarSupervisorPending,
  whenSidecarSupervisor,
} from '../../src/sidecar/handle.js';

// handle.ts 的 whenSidecarSupervisor 是 skill-loader 等 RPC 薄客户端的
// 启动竞态出口：渲染进程的技能列表请求可能早于 sidecar 就绪到达。
// 这些测试用真实的模块状态（非 mock）验证等待/超时/关闭三条路径。

const fakeSupervisor = { note: 'stand-in' } as never;

describe('sidecar handle / whenSidecarSupervisor', () => {
  const savedEnv = process.env.STEERABLE_USE_SIDECAR;

  beforeEach(() => {
    delete process.env.STEERABLE_USE_SIDECAR;
    setSidecarSupervisor(null);
    setSidecarSupervisorPending(null);
  });

  afterEach(() => {
    if (savedEnv === undefined) delete process.env.STEERABLE_USE_SIDECAR;
    else process.env.STEERABLE_USE_SIDECAR = savedEnv;
    setSidecarSupervisor(null);
    setSidecarSupervisorPending(null);
  });

  it('已就绪时立即返回 live handle，不看 pending', async () => {
    setSidecarSupervisor(fakeSupervisor);
    setSidecarSupervisorPending(new Promise(() => {})); // 永不解决也不影响
    await expect(whenSidecarSupervisor(50)).resolves.toBe(fakeSupervisor);
  });

  it('启动竞态：pending 晚解决时等待并拿到 handle', async () => {
    setSidecarSupervisorPending(
      new Promise((resolve) => setTimeout(() => resolve(fakeSupervisor), 30)),
    );
    const t0 = Date.now();
    await expect(whenSidecarSupervisor(2_000)).resolves.toBe(fakeSupervisor);
    expect(Date.now() - t0).toBeGreaterThanOrEqual(25);
  });

  it('等待超时返回 null（调用方退化为无 sidecar 路径，而不是挂死）', async () => {
    setSidecarSupervisorPending(new Promise(() => {})); // 永不解决
    const t0 = Date.now();
    await expect(whenSidecarSupervisor(40)).resolves.toBeNull();
    expect(Date.now() - t0).toBeLessThan(1_000);
  });

  it('boot 失败（pending 解决为 null）返回 null', async () => {
    setSidecarSupervisorPending(Promise.resolve(null));
    await expect(whenSidecarSupervisor(2_000)).resolves.toBeNull();
  });

  it('boot 拒绝（异常）时不抛出，返回 null', async () => {
    setSidecarSupervisorPending(Promise.reject(new Error('python missing')));
    await expect(whenSidecarSupervisor(2_000)).resolves.toBeNull();
  });

  it('无 pending（未启动或已结束）返回 null', async () => {
    await expect(whenSidecarSupervisor(50)).resolves.toBeNull();
  });

  it('STEERABLE_USE_SIDECAR=0 时直接返回 null，不等 pending', async () => {
    process.env.STEERABLE_USE_SIDECAR = '0';
    setSidecarSupervisor(fakeSupervisor);
    setSidecarSupervisorPending(new Promise(() => {}));
    await expect(whenSidecarSupervisor(50)).resolves.toBeNull();
  });

  it('getSidecarSupervisor 在禁用时不透传 handle', () => {
    process.env.STEERABLE_USE_SIDECAR = '0';
    setSidecarSupervisor(fakeSupervisor);
    expect(getSidecarSupervisor()).toBeNull();
  });
});
