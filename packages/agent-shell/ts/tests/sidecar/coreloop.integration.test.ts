/**
 * A4 live canary: real sidecar + real LLM (Ollama cloud) + real reverse
 * channel, end to end.
 *
 * Skipped by default. Run with:
 *
 *   STEERABLE_SIDECAR_TEST=1 \
 *   STEERABLE_SIDECAR_PYTHON=/path/to/steerable-framework/.venv/bin/python3 \
 *   STEERABLE_SMOKE_MODEL=gpt-oss:20b-cloud \
 *     npx vitest run tests/sidecar/coreloop.integration.test.ts
 *
 * The test advertises two host tools (list_dir / read_file, sandboxed to a
 * temp dir), asks the model a question that forces tool use, and asserts the
 * whole loop: CoreLoop drives rounds in Python → tool calls arrive here over
 * the reverse channel → results go back → the final answer reflects the real
 * file contents.
 */

import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { SidecarSupervisor, type SidecarStreamChunk } from '../../src/sidecar';
import { createToolInvokeHandler } from '../../src/sidecar/reverse-tools.js';
import type { ToolRouter } from '../../src/tool-router.js';

const PY = process.env.STEERABLE_SIDECAR_PYTHON;
const ENABLED = process.env.STEERABLE_SIDECAR_TEST === '1';
const MODEL = process.env.STEERABLE_SMOKE_MODEL ?? 'gpt-oss:20b-cloud';
const CANARY = 'canary-42';

const guard = ENABLED && PY ? describe : describe.skip;

// This canary needs a real Ollama(-cloud) model, so it stays opt-in forever
// and never runs in CI; the keyless loopback-mock-LLM e2e tier under
// tests/e2e/ owns the CI-runnable coverage. Say so visibly when skipping.
if (!(ENABLED && PY)) {
  console.warn(
    '[coreloop.integration] skipped: needs real Ollama — opt in with ' +
      'STEERABLE_SIDECAR_TEST=1, STEERABLE_SIDECAR_PYTHON and STEERABLE_SMOKE_MODEL',
  );
}

guard('CoreLoop live canary (real sidecar + Ollama cloud)', () => {
  it('drives a tool round-trip through the reverse channel', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'coreloop-smoke-'));
    writeFileSync(path.join(dir, 'canary.txt'), CANARY);

    // Minimal host tool surface, sandboxed to the temp dir.
    const executed: Array<{ name: string; arguments: Record<string, unknown> }> = [];
    const toolRouter = {
      execute: async (call: { name: string; arguments: Record<string, unknown> }) => {
        executed.push(call);
        const target = String(call.arguments.path ?? '');
        if (!target.startsWith(dir)) {
          return { success: false, error: `path outside sandbox: ${target}` };
        }
        if (call.name === 'list_dir') {
          const { readdirSync } = await import('node:fs');
          return { success: true, data: { value: readdirSync(target).join('\n') } };
        }
        if (call.name === 'read_file') {
          const { readFileSync } = await import('node:fs');
          return { success: true, data: { value: readFileSync(target, 'utf8') } };
        }
        return { success: false, error: `Unknown tool: ${call.name}` };
      },
    } as unknown as ToolRouter;

    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });
    supervisor.onReverseRequest('tool.invoke', createToolInvokeHandler({ toolRouter }));

    const chunks: SidecarStreamChunk[] = [];
    let text = '';
    let done: { ok: boolean; status?: string; reason?: string } | null = null;
    let streamError: string | null = null;

    try {
      await supervisor.streamChat(
        {
          provider: 'ollama',
          model: MODEL,
          baseUrl: 'http://127.0.0.1:11434/v1',
          useCoreLoop: true,
          toolsViaHost: true,
          messages: [
            {
              role: 'user',
              content:
                `List the files in ${dir}, then read the file canary.txt in that ` +
                `directory, and tell me its exact contents. Use the tools.`,
            },
          ],
          tools: [
            {
              type: 'function',
              function: {
                name: 'list_dir',
                description: 'List files in a directory',
                parameters: {
                  type: 'object',
                  properties: { path: { type: 'string' } },
                  required: ['path'],
                },
              },
            },
            {
              type: 'function',
              function: {
                name: 'read_file',
                description: 'Read a UTF-8 text file and return its contents',
                parameters: {
                  type: 'object',
                  properties: { path: { type: 'string' } },
                  required: ['path'],
                },
              },
            },
          ],
        },
        {
          onChunk: (chunk) => {
            chunks.push(chunk);
            if (chunk.delta) text += chunk.delta;
          },
          onDone: (d) => {
            done = d;
          },
          onError: (e) => {
            streamError = `${e.kind}: ${e.message}`;
          },
        },
      );

      // streamChat resolves once the stream is *accepted*; wait for done.
      const deadline = Date.now() + 150_000;
      while (!done && !streamError && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 250));
      }

      console.info('[canary] model:', MODEL);
      console.info('[canary] executed tools:', JSON.stringify(executed));
      console.info('[canary] tool events on wire:', chunks.filter((c) => c.toolCall || c.toolResult).length);
      console.info('[canary] final text:', text.slice(0, 400));
      console.info('[canary] done:', JSON.stringify(done), 'error:', streamError);

      expect(streamError).toBeNull();
      expect(done).not.toBeNull();
      expect(done!.ok).toBe(true);
      expect(done!.status).toBe('completed');

      // The loop actually executed host tools via the reverse channel…
      expect(executed.length).toBeGreaterThan(0);
      expect(executed.some((c) => c.name === 'read_file')).toBe(true);
      // …and the final answer reflects the real file contents (grounding).
      expect(text).toContain(CANARY);
    } finally {
      await supervisor.shutdown();
      rmSync(dir, { recursive: true, force: true });
    }
  }, 180_000);

  it('injects worldState sections (Wave 2 wire proof)', async () => {
    const supervisor = await SidecarSupervisor.start({
      pythonExecutable: PY!,
      bootTimeoutMs: 30_000,
      healthIntervalMs: 0,
    });

    const chunks: SidecarStreamChunk[] = [];
    let done: { ok: boolean; status?: string } | null = null;
    let streamError: string | null = null;

    try {
      await supervisor.streamChat(
        {
          provider: 'ollama',
          model: MODEL,
          baseUrl: 'http://127.0.0.1:11434/v1',
          useCoreLoop: true,
          chatId: `worldstate-canary-${Date.now()}`,
          worldState: {
            time: {
              local: '2026-08-29T10:20',
              timezone: 'Asia/Shanghai',
              weekday: '周六',
            },
          },
          messages: [{ role: 'user', content: 'Reply with a short greeting.' }],
        },
        {
          onChunk: (chunk) => chunks.push(chunk),
          onDone: (d) => {
            done = d;
          },
          onError: (e) => {
            streamError = `${e.kind}: ${e.message}`;
          },
        },
      );

      const deadline = Date.now() + 120_000;
      while (!done && !streamError && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 250));
      }

      expect(streamError).toBeNull();
      expect(done).not.toBeNull();
      expect(done!.ok).toBe(true);

      // Deterministic wire proof: the sidecar's WorldStateHooks injected the
      // <world-state> fragment on round 0 and the loop recorded the
      // hook_action, which the sidecar forwards as a notice. The assertion
      // is on the wire, not the model's text — no model jitter.
      const worldStateNotice = chunks.find(
        (c) =>
          c.notice?.kind === 'hook_action' &&
          (c.notice as Record<string, unknown>).action === 'world_state',
      );
      expect(worldStateNotice).toBeDefined();
    } finally {
      await supervisor.shutdown();
    }
  }, 150_000);
});
