import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const PROC_TEST_TIMEOUT = 20_000;
import {
  SidecarNotReadyError,
  SidecarProcess,
  SidecarStartError,
} from '../src/sidecar.js';

const FAKE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fake-sidecar.mjs');

function fake(env: NodeJS.ProcessEnv = {}, opts = {}) {
  return new SidecarProcess({
    python: process.execPath,
    entryModule: null,
    args: [FAKE],
    env,
    readyTimeoutMs: 8_000,
    shutdownGraceMs: 1_000,
    ...opts,
  });
}

describe('SidecarProcess', () => {
  it('spawns, becomes ready, answers requests, shuts down gracefully', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const p = fake();
    expect(p.isReady).toBe(false);
    await p.start();
    expect(p.isReady).toBe(true);
    expect(p.ready?.version).toBe('0.0.0-fake');
    const health = await p.request<{ status: string }>('system.ping');
    expect(health.status).toBe('ok');
    await p.close();
    expect(p.isReady).toBe(false);
    await expect(p.request('system.ping')).rejects.toBeInstanceOf(SidecarNotReadyError);
  });

  it('fails start when lifecycle.ready never arrives', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const p = fake({ FAKE_NO_READY: '1' }, { restart: false });
    await expect(p.start()).rejects.toBeInstanceOf(SidecarStartError);
    await p.close();
  });

  it('auto-restarts after an unexpected exit and serves again', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const p = fake({ FAKE_CRASH_MS: '150' }, { restart: { maxRestarts: 2, backoffMs: 10 } });
    await p.start();
    await new Promise<void>((resolve) => p.once('exit', () => resolve()));
    // The crash at ~150ms triggers one restart; the next ready proves it.
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('never re-ready')), 3_000);
      p.once('ready', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    const health = await p.request<{ status: string }>('system.ping');
    expect(health.status).toBe('ok');
    await p.close();
  });

  it('emits dead after exhausting the restart budget', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const p = fake(
      { FAKE_CRASH_MS: '80' },
      { restart: { maxRestarts: 1, backoffMs: 10 } },
    );
    await p.start();
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('never dead')), 5_000);
      p.once('dead', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    expect(p.isReady).toBe(false);
    await p.close();
  });

  it('close() during a crash-restart cycle does not resurrect the process', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const p = fake({ FAKE_CRASH_MS: '60' }, { restart: { maxRestarts: 5, backoffMs: 50 } });
    await p.start();
    await new Promise<void>((resolve) => p.once('exit', () => resolve()));
    await p.close();
    // Backoff is 50ms; if a restart slipped through it would boot within
    // 300ms. closing=true must suppress it.
    await new Promise((r) => setTimeout(r, 300));
    expect(p.pid).toBeUndefined();
    expect(p.isReady).toBe(false);
  });
});
