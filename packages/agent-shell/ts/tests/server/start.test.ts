import { createServer } from 'node:http';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  createBsServer: vi.fn(),
  createHostRuntime: vi.fn(),
  runtimeStart: vi.fn(),
  runtimeShutdown: vi.fn(),
}));

vi.mock('../../src/server/http-server.js', () => ({
  createBsServer: mocks.createBsServer,
}));
vi.mock('../../src/host/runtime.js', () => ({
  createHostRuntime: mocks.createHostRuntime,
}));

import { startBsHost, type BsMiddleware } from '../../src/server/start.js';

describe('startBsHost', () => {
  let webDistDir: string;
  let previousWebDist: string | undefined;

  beforeEach(() => {
    vi.clearAllMocks();
    previousWebDist = process.env.DEEPPATH_WEB_DIST;
    webDistDir = mkdtempSync(path.join(tmpdir(), 'bs-start-web-'));
    writeFileSync(path.join(webDistDir, 'index.html'), '<html></html>');
    process.env.DEEPPATH_WEB_DIST = webDistDir;
    mocks.runtimeShutdown.mockResolvedValue(undefined);
    mocks.createHostRuntime.mockReturnValue({
      localExecutor: {},
      localScriptRegistry: {},
      terminalManager: {},
      packHandles: new Map(),
      localBackendRouter: {},
      approvalBridge: {},
      askUserBridge: {},
      maybeExecInTerminal: vi.fn(),
      start: mocks.runtimeStart,
      shutdown: mocks.runtimeShutdown,
    });
    mocks.createBsServer.mockImplementation(() =>
      createServer((_req, res) => res.end('ok')),
    );
  });

  afterEach(() => {
    if (previousWebDist === undefined) delete process.env.DEEPPATH_WEB_DIST;
    else process.env.DEEPPATH_WEB_DIST = previousWebDist;
    rmSync(webDistDir, { recursive: true, force: true });
  });

  it('passes composition options, starts runtime, and shuts down once', async () => {
    const middleware: BsMiddleware = async () => 'pass';
    const scope = { tenantId: 'tenant-1', userId: 'user-1' };
    const handle = await startBsHost({
      host: '127.0.0.1',
      port: 0,
      authToken: 'fixed-token',
      middleware: [middleware],
      scope,
    });

    expect(handle.host).toBe('127.0.0.1');
    expect(handle.port).toBeGreaterThan(0);
    expect(mocks.createBsServer).toHaveBeenCalledWith(
      expect.objectContaining({
        authToken: 'fixed-token',
        middleware: [middleware],
        webDistDir,
      }),
    );
    expect(mocks.createHostRuntime).toHaveBeenCalledWith(
      expect.objectContaining({ scope }),
    );
    expect(mocks.runtimeStart).toHaveBeenCalledOnce();

    await Promise.all([handle.shutdown(), handle.shutdown()]);
    expect(mocks.runtimeShutdown).toHaveBeenCalledOnce();
  });
});
