import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SidecarSandboxPosture } from '@/lib/local-api';

// 沙箱诚实告知的渲染层锁定：收容失败必须说出「无法收容、已拒绝启动」，
// 而不是谎称进程仍在无隔离运行。posture/API 那一层由
// tests/sidecar/supervisor-sandbox.test.ts 与 sandbox-posture.e2e.test.ts 覆盖；
// 这里只管「用户看不看得见」。

const getSidecarSandboxPosture = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({ isElectron: () => true }));
vi.mock('@/lib/local-api', () => ({
  getSidecarSandboxPosture: () => getSidecarSandboxPosture(),
}));

const { SecuritySettingsPanel } = await import('./SecuritySettingsPanel');

function posture(over: Partial<SidecarSandboxPosture>): SidecarSandboxPosture {
  return { backend: 'none', enforcement: 'none', reason: 'platform_unsupported', ...over };
}

beforeEach(() => {
  getSidecarSandboxPosture.mockReset();
});

afterEach(cleanup);

describe('SecuritySettingsPanel — layer-1 态势披露', () => {
  it.each([
    ['platform_unsupported', '当前平台没有可用的进程沙箱后端'],
    ['seatbelt_missing', '未找到 /usr/bin/sandbox-exec'],
    ['profile_failed', '沙箱配置（Seatbelt profile）生成失败'],
    ['wrap_failed', 'Linux 进程沙箱'],
    ['helper_missing', '未找到 win-spawn-helper.exe'],
  ] as const)('收容失败 %s：告知已拒绝启动，不谎称仍在跑', async (reason, detail) => {
    getSidecarSandboxPosture.mockResolvedValue({ posture: posture({ reason }) });
    render(<SecuritySettingsPanel />);

    const headline = await screen.findByText(/无法收容、已拒绝启动 —/);
    expect(headline.textContent).toContain(detail);
    expect(screen.getByText(/没有启动/)).toBeTruthy();
    expect(screen.queryByText(/未沙箱 —/)).toBeNull();
    expect(screen.getByText(/STEERABLE_SIDECAR_SANDBOX=0/)).toBeTruthy();
  });

  it('手动关闭中性展示，不冒充告警也不谎称已隔离', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ reason: 'disabled_by_env' }),
    });
    render(<SecuritySettingsPanel />);

    expect((await screen.findByText(/已手动关闭 —/)).textContent).toContain(
      'STEERABLE_SIDECAR_SANDBOX=0',
    );
    expect(screen.queryByText(/无法收容、已拒绝启动 —/)).toBeNull();
    expect(screen.getByText(/无操作系统隔离运行/)).toBeTruthy();
  });

  it('Seatbelt 生效时记为「部分」，不谎称完全强制', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ backend: 'seatbelt', enforcement: 'partial', reason: 'active' }),
    });
    render(<SecuritySettingsPanel />);

    expect(await screen.findByText(/Seatbelt · 部分强制/)).toBeTruthy();
    expect(screen.getByText(/不识别主机名/)).toBeTruthy();
    expect(screen.queryByText(/无法收容、已拒绝启动 —/)).toBeNull();
  });

  it('bwrap 生效时记为部分强制', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ backend: 'bwrap', enforcement: 'partial', reason: 'active' }),
    });
    render(<SecuritySettingsPanel />);
    expect(await screen.findByText(/bwrap · 部分强制/)).toBeTruthy();
  });

  it('sidecar 未就绪时不冒充任何一种姿态', async () => {
    getSidecarSandboxPosture.mockRejectedValue(new Error('503'));
    render(<SecuritySettingsPanel />);

    expect(await screen.findByText(/sidecar 未就绪/)).toBeTruthy();
    expect(screen.queryByText(/无法收容、已拒绝启动 —/)).toBeNull();
    expect(screen.queryByText(/部分强制/)).toBeNull();
  });

  it('出网退回端口级时披露原因（此前只有主进程日志可见）', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ backend: 'seatbelt', enforcement: 'partial', reason: 'active' }),
      egress: {
        mode: 'port-only-fallback',
        reason: '检测到系统/环境代理（127.0.0.1:7890），按主机管控已退回端口级',
      },
    });
    render(<SecuritySettingsPanel />);

    expect(await screen.findByText(/已退回端口级/)).toBeTruthy();
    expect(screen.getByText(/127\.0\.0\.1:7890/)).toBeTruthy();
  });

  it('按主机代理生效时说明逐项放行语义', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ backend: 'seatbelt', enforcement: 'partial', reason: 'active' }),
      egress: { mode: 'per-host-proxy', reason: null },
    });
    render(<SecuritySettingsPanel />);

    expect(await screen.findByText(/按主机白名单代理生效中/)).toBeTruthy();
    expect(screen.getByText(/逐项放行/)).toBeTruthy();
  });

  it('无 egress 字段（旧后端）时不渲染出网行', async () => {
    getSidecarSandboxPosture.mockResolvedValue({
      posture: posture({ backend: 'seatbelt', enforcement: 'partial', reason: 'active' }),
    });
    render(<SecuritySettingsPanel />);

    await screen.findByText(/Seatbelt · 部分强制/);
    expect(screen.queryByText(/出网管控/)).toBeNull();
  });
});
