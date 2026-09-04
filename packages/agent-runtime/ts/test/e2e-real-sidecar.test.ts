/**
 * End-to-end: AgentRuntime (TS) → REAL Python sidecar process → CoreLoop →
 * OpenAICompatProvider → local mock OpenAI HTTP server → reverse-channel
 * `tool.invoke` back into this test → second LLM round → terminal done.
 *
 * Every layer is real except the model itself: the sidecar is the actual
 * `steerable_sidecar` package from this repo's uv venv, driven over stdio
 * JSON-RPC; the LLM is a loopback HTTP server speaking OpenAI SSE.
 *
 * Self-skips when the Python environment is unavailable (e.g. a TS-only CI
 * runner that never ran `uv sync`) — an E2E that cannot boot its subject
 * must say so, not fail. CI sets STEERABLE_E2E_REQUIRED=1 to turn that skip
 * into a hard failure so the full-stack coverage cannot silently rot.
 */
import { execFileSync } from 'node:child_process';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import { AgentRuntime } from '../src/runtime.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../../..');
const VENV_PYTHON = path.join(REPO, '.venv', 'bin', 'python');

function pythonSidecarAvailable(): boolean {
  try {
    execFileSync(VENV_PYTHON, ['-c', 'import steerable_sidecar'], {
      stdio: 'pipe',
      timeout: 15_000,
    });
    return true;
  } catch {
    return false;
  }
}

const AVAILABLE = pythonSidecarAvailable();
// CI sets STEERABLE_E2E_REQUIRED=1 so a sidecar-incapable runner fails hard
// instead of silently skipping the full-stack coverage.
const E2E_REQUIRED = process.env.STEERABLE_E2E_REQUIRED === '1';
const describeE2E = AVAILABLE ? describe : describe.skip;

/** Deterministic mock of POST /v1/chat/completions (SSE). */
function startMockOpenAI(): Promise<{ server: Server; baseUrl: string; requests: unknown[] }> {
  const requests: unknown[] = [];
  const server = createServer((req, res) => {
    if (req.method !== 'POST' || !req.url?.endsWith('/chat/completions')) {
      res.writeHead(404).end();
      return;
    }
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      requests.push(JSON.parse(body));
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      const send = (obj: unknown) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
      if (requests.length === 1) {
        // First turn: call the host's `echo` tool.
        send({
          choices: [{
            index: 0,
            delta: {
              tool_calls: [{
                index: 0,
                id: 'call_e2e_1',
                type: 'function',
                function: { name: 'echo', arguments: '' },
              }],
            },
          }],
        });
        send({
          choices: [{
            index: 0,
            delta: { tool_calls: [{ index: 0, function: { arguments: '{"text":"from-python-coreloop"}' } }] },
          }],
        });
        send({ choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] });
      } else {
        // Second turn (tool result in history): final answer.
        send({ choices: [{ index: 0, delta: { content: 'E2E_OK' } }] });
        send({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
      }
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      resolve({ server, baseUrl: `http://127.0.0.1:${port}/v1`, requests });
    });
  });
}

describeE2E('E2E: TS runtime ↔ real Python sidecar', () => {
  let mock: { server: Server; baseUrl: string; requests: unknown[] };
  let rt: AgentRuntime | null = null;

  beforeAll(async () => {
    mock = await startMockOpenAI();
  }, 30_000);

  afterEach(async () => {
    await rt?.close();
    rt = null;
  });

  it('runs a full tool-calling turn through the real CoreLoop', { timeout: 60_000 }, async () => {
    const toolCallsSeen: unknown[] = [];
    rt = new AgentRuntime({
      python: VENV_PYTHON,
      cwd: REPO,
      readyTimeoutMs: 20_000,
      restart: false,
      tools: {
        echo: async (args: Record<string, unknown>) => {
          toolCallsSeen.push(args);
          return { success: true, result: `host-says:${String(args.text)}` };
        },
      },
    });
    await rt.start();
    expect(rt.process.isReady).toBe(true);

    const health = await rt.ping();
    expect(health.status).toBe('ok');

    const handle = await rt.chatStream({
      provider: 'openai_compat',
      model: 'mock-model',
      baseUrl: mock.baseUrl,
      apiKey: 'e2e-not-a-real-key',
      messages: [{ role: 'user', content: 'call the echo tool' }],
      // Desktop-style deployment: tool calls execute on the host via the
      // reverse channel (`tool.invoke`), not in the sidecar's registry.
      toolsViaHost: true,
      tools: [{
        type: 'function',
        function: {
          name: 'echo',
          description: 'echo back text',
          parameters: {
            type: 'object',
            properties: { text: { type: 'string' } },
            required: ['text'],
          },
        },
      }],
    });

    const types: string[] = [];
    let text = '';
    for await (const ev of handle.events as AsyncIterable<SSEEvent>) {
      types.push(ev.type);
      if (ev.type === 'content') text += ev.content ?? '';
    }

    // The Python CoreLoop called our host tool over the reverse channel…
    expect(toolCallsSeen).toEqual([{ text: 'from-python-coreloop' }]);
    // …received its result, made a second LLM round, and streamed the answer.
    expect(text).toContain('E2E_OK');
    expect(types).toContain('tool_call');
    expect(types).toContain('tool_result');
    expect(types[types.length - 1]).toBe('done');
    await expect(handle.done).resolves.toEqual({ status: 'completed', cancelled: false });

    // Two LLM rounds happened; the second carries the tool result back.
    expect(mock.requests).toHaveLength(2);
    const second = mock.requests[1] as { messages: Array<{ role: string; content?: unknown }> };
    const toolMsg = second.messages.find((m) => m.role === 'tool');
    expect(JSON.stringify(toolMsg ?? {})).toContain('host-says:from-python-coreloop');
  });

  it('cooperative cancel winds down a real in-flight stream', { timeout: 60_000 }, async () => {
    rt = new AgentRuntime({
      python: VENV_PYTHON,
      cwd: REPO,
      readyTimeoutMs: 20_000,
      restart: false,
    });
    await rt.start();
    // The mock answers every request; cancel races the turn. Use a slow
    // second response is not available here — instead cancel immediately
    // after stream open and accept either a completed-fast turn or a
    // cancelled one, asserting only that the terminal state is coherent.
    const handle = await rt.chatStream({
      provider: 'openai_compat',
      model: 'mock-model',
      baseUrl: mock.baseUrl,
      apiKey: 'e2e-not-a-real-key',
      messages: [{ role: 'user', content: 'anything' }],
    });
    await rt.cancelChat(handle.streamId);
    const terminal = await handle.done;
    expect(['completed', 'cancelled']).toContain(terminal.status);
    if (terminal.status === 'cancelled') expect(terminal.cancelled).toBe(true);
  });
});

if (!AVAILABLE && E2E_REQUIRED) {
  // The suite above skips; register the failure explicitly so CI sees it.
  describe('E2E: TS runtime ↔ real Python sidecar (required)', () => {
    it('requires a sidecar-capable python', () => {
      throw new Error(
        `[e2e-real-sidecar] ${VENV_PYTHON} cannot import steerable_sidecar and ` +
          'STEERABLE_E2E_REQUIRED=1 forbids skipping (run `uv sync` at the repo root)',
      );
    });
  });
} else if (!AVAILABLE) {
  // Visible in the test report instead of a silent skip.
  console.warn(
    `[e2e-real-sidecar] skipped: ${VENV_PYTHON} cannot import steerable_sidecar ` +
      '(run `uv sync` at the repo root to enable; CI sets STEERABLE_E2E_REQUIRED=1 ' +
      'to make this a hard failure)',
  );
}
