/**
 * Layer-3 态势构造（`buildExecSandbox`）单元测试。
 *
 * 重点是 `requireFull`：它在执行前拒掉所有达不到 full 的调用，所以「这台机器
 * 能不能达到 full」必须来自 sidecar 的探测，而不是宿主按平台猜。曾按 egress
 * 代理是否活跃推导过，结果代理默认开之后，Linux 上每次 shell 调用都被拒。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('electron-log', () => ({
  default: { info: () => {}, warn: () => {}, error: () => {} },
}));
vi.mock('../../src/llm/index.js', () => ({
  llmService: {
    getSettings: () => ({ baseUrl: 'https://api.deepseek.com/v1' }),
  },
}));

let proxyEndpoint: string | null = null;
vi.mock('../../src/sidecar/egress-proxy.js', () => ({
  getActiveEgressProxyEndpoint: () => proxyEndpoint,
}));

import {
  buildExecSandbox,
  deriveEgressAllowListFromBaseUrl,
  getExecSandboxCapability,
  parseExecPolicy,
  probeExecSandboxCapability,
} from '../../src/sidecar/exec-sandbox.js';

/** `probeExecSandboxCapability` 只用到 supervisor 的 `call`。 */
function supervisorReturning(result: unknown) {
  return { call: vi.fn(async () => result) } as never;
}

function supervisorThrowing() {
  return {
    call: vi.fn(async () => {
      throw new Error('sidecar is not answering');
    }),
  } as never;
}

describe('buildExecSandbox', () => {
  beforeEach(async () => {
    proxyEndpoint = null;
    await probeExecSandboxCapability(supervisorThrowing());
  });

  it('requires full enforcement only when the probe reached full', async () => {
    await probeExecSandboxCapability(
      supervisorReturning({ backend: 'seatbelt', enforcement: 'full' }),
    );
    expect(buildExecSandbox([]).requireFull).toBe(true);
  });

  it('does not require full on a backend that reports partial', async () => {
    // bwrap/Landlock 没有按主机 egress 管控，带 network 就只能报 partial。
    await probeExecSandboxCapability(
      supervisorReturning({ backend: 'bwrap', enforcement: 'partial' }),
    );
    expect(buildExecSandbox([]).requireFull).toBe(false);
  });

  it('does not require full when the probe itself failed', async () => {
    // 一次 RPC 抖动不该把整个 shell 面判死。
    await probeExecSandboxCapability(supervisorThrowing());
    expect(getExecSandboxCapability()).toBeNull();
    expect(buildExecSandbox([]).requireFull).toBe(false);
  });

  it('always requires a backend, so nothing runs unconfined', () => {
    expect(buildExecSandbox([]).requireBackend).toBe(true);
  });

  it('pins egress to the proxy endpoint when the proxy is live', () => {
    proxyEndpoint = '127.0.0.1:8899';
    expect(buildExecSandbox([]).allowedHosts).toEqual(['127.0.0.1:8899']);
  });

  it('falls back to the provider endpoint without the proxy', () => {
    expect(buildExecSandbox([]).allowedHosts).toEqual(['api.deepseek.com']);
  });

  it('carries the writable roots it was given', () => {
    expect(buildExecSandbox(['/work/proj']).writableRoots).toEqual(['/work/proj']);
  });

  it('full policy turns confinement off', () => {
    const params = buildExecSandbox(['/work/proj'], { policy: 'full' });
    expect(params.enabled).toBe(false);
    expect(params.requireBackend).toBe(false);
    expect(params.requireFull).toBe(false);
    expect(params.writableRoots).toEqual([]);
  });
});

describe('parseExecPolicy', () => {
  it('only treats exact full as full access', () => {
    expect(parseExecPolicy('full')).toBe('full');
    expect(parseExecPolicy('workspace')).toBe('workspace');
    expect(parseExecPolicy(undefined)).toBe('workspace');
    expect(parseExecPolicy('danger-full-access')).toBe('workspace');
  });
});

describe('probeExecSandboxCapability', () => {
  it('asks with the egress arguments a turn will actually send', async () => {
    proxyEndpoint = '127.0.0.1:8899';
    const supervisor = supervisorReturning({
      backend: 'seatbelt',
      enforcement: 'full',
    });
    await probeExecSandboxCapability(supervisor);

    expect(supervisor.call).toHaveBeenCalledWith('sandbox.describe', {
      network: true,
      allowedHosts: ['127.0.0.1:8899'],
    });
  });
});

describe('deriveEgressAllowListFromBaseUrl', () => {
  it('keeps an explicit port', () => {
    expect(deriveEgressAllowListFromBaseUrl('http://127.0.0.1:11434/v1')).toEqual([
      '127.0.0.1:11434',
    ]);
  });

  it('is undefined when nothing parses — hardening must not brick the LLM path', () => {
    expect(deriveEgressAllowListFromBaseUrl(undefined)).toBeUndefined();
    expect(deriveEgressAllowListFromBaseUrl('not a url')).toBeUndefined();
  });
});
