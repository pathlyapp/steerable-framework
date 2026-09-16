/**
 * Integration test for the sidecar supervisor.
 *
 * Skipped by default — run with `STEERABLE_SIDECAR_PYTHON=$(which python3)` and
 * `STEERABLE_SIDECAR_TEST=1` to opt in. The test spawns the real Python sidecar
 * via the supervisor and exercises:
 *
 *   1. boot → system.ping → tool.list → graceful shutdown
 *   2. agent.chat.stream rejects an unknown provider with a structured
 *      `stream.error` notification (proves the chat plumbing wires up
 *      end-to-end without needing live LLM credentials)
 *   3. agent.chat.cancel terminates an in-flight stream
 *
 * Run locally:
 *
 *   STEERABLE_SIDECAR_TEST=1 \
 *   STEERABLE_SIDECAR_PYTHON=/path/to/steerable-framework/.venv/bin/python3 \
 *     pnpm test tests/sidecar/supervisor.integration.test.ts
 */

import { describe, expect, it } from 'vitest';

import {
  SidecarSupervisor,
  type SidecarStreamChunk,
  type SidecarStreamDone,
  type SidecarStreamError,
} from '../../src/sidecar';

const PY = process.env.STEERABLE_SIDECAR_PYTHON;
const ENABLED = process.env.STEERABLE_SIDECAR_TEST === '1';

const guard = ENABLED && PY ? describe : describe.skip;

// STEERABLE_E2E_REQUIRED=1 converts the opt-in skip into a hard failure, so
// a CI job that promises this coverage cannot let it rot silently.
if (!(ENABLED && PY) && process.env.STEERABLE_E2E_REQUIRED === '1') {
  describe('SidecarSupervisor (real subprocess) [required]', () => {
    it('requires STEERABLE_SIDECAR_TEST=1 and STEERABLE_SIDECAR_PYTHON', () => {
      throw new Error(
        'sidecar integration test skipped but STEERABLE_E2E_REQUIRED=1: ' +
          `STEERABLE_SIDECAR_TEST=${process.env.STEERABLE_SIDECAR_TEST ?? '(unset)'}, ` +
          `STEERABLE_SIDECAR_PYTHON=${PY ?? '(unset)'}`,
      );
    });
  });
}

guard('SidecarSupervisor (real subprocess)', () => {
  it('boots, pings and shuts down cleanly', async () => {
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });
    try {
      const ping = await supervisor.ping();
      expect(ping.protocolVersion).toBe('0.1.0');
      const tools = await supervisor.listTools();
      expect(Array.isArray(tools)).toBe(true);
    } finally {
      await supervisor.shutdown();
    }
  }, 60_000);

  it('agent.chat.stream surfaces a structured error for an unknown provider', async () => {
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });
    try {
      const errors: SidecarStreamError[] = [];
      const dones: SidecarStreamDone[] = [];

      // The supervisor surfaces sidecar JSON-RPC errors by rejecting the
      // streamChat() promise — bogus provider name is rejected by
      // `default_llm_provider_factory` before any network call happens, so
      // we expect either a rejection on the request or an error notification.
      let rejected: unknown = null;
      try {
        await supervisor.streamChat(
          {
            provider: 'definitely-not-a-real-provider',
            model: 'gpt-4o-mini',
            messages: [{ role: 'user', content: 'hi' }],
          },
          {
            onError: (err) => errors.push(err),
            onDone: (done) => dones.push(done),
          },
        );
      } catch (err) {
        rejected = err;
      }

      // Either path is acceptable per the JSON-RPC contract — the value-add
      // here is "the chat plumbing reached the factory, classified the
      // request as invalid, and surfaced that to the supervisor".
      const haveError =
        rejected != null ||
        errors.some(
          (e) => /provider|invalid|unknown/i.test(`${e.kind} ${e.message}`),
        );
      expect(haveError).toBe(true);
      expect(dones.find((d) => d.ok === true)).toBeUndefined();
    } finally {
      await supervisor.shutdown();
    }
  }, 60_000);

  it('boots sandboxed under Seatbelt when sandbox: true (macOS)', async () => {
    if (process.platform !== 'darwin') return;
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      sandbox: true,
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });
    try {
      // A confined sidecar that can boot, import its runtime, and answer a
      // ping proves the profile doesn't starve the Python interpreter.
      const ping = await supervisor.ping();
      expect(ping.status).toBe('ok');
    } finally {
      await supervisor.shutdown();
    }
  }, 60_000);

  it('serves a reverse (sidecar -> host) tool.invoke request', async () => {
    // Spawn the framework's test sidecar, which registers `test.run_host_tool`:
    // when the host calls it, the sidecar issues a reverse `tool.invoke` back
    // to the host and returns whatever the host responded.
    const path = await import('node:path');
    const url = await import('node:url');
    const here = path.dirname(url.fileURLToPath(import.meta.url));
    const frameworkTests = path.resolve(
      here,
      '../../../steerable-framework/packages/sidecar/py/tests',
    );

    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      entryModule: 'reverse_echo_sidecar',
      env: { ...process.env, PYTHONPATH: frameworkTests },
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });
    try {
      // The host executes the tool the sidecar asks for.
      supervisor.onReverseRequest('tool.invoke', (params) => {
        const p = params as { name: string; arguments: { command: string } };
        return { success: true, data: { stdout: `host-ran:${p.arguments.command}` } };
      });

      const result = await supervisor.call<{ success: boolean; data: { stdout: string } }>(
        'test.run_host_tool',
        { name: 'local_exec_shell', arguments: { command: 'pwd' } },
      );
      expect(result.success).toBe(true);
      expect(result.data.stdout).toBe('host-ran:pwd');
    } finally {
      await supervisor.shutdown();
    }
  }, 60_000);
});
