import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import { AgentRuntime } from '../src/runtime.js';
import {
  createChatStreamTransport,
  createSessionTransport,
} from '../src/transports.js';

// These tests spawn real child processes; under parallel-file load process
// boot can exceed vitest's 5s default.
const PROC_TEST_TIMEOUT = 20_000;

const FAKE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fake-sidecar.mjs');

function runtime(env: NodeJS.ProcessEnv = {}) {
  return new AgentRuntime({
    python: process.execPath,
    entryModule: null,
    args: [FAKE],
    env,
    readyTimeoutMs: 8_000,
    shutdownGraceMs: 1_000,
    restart: false,
  });
}

describe('agent-ui transports', () => {
  let rt: AgentRuntime | null = null;
  afterEach(async () => {
    await rt?.close();
    rt = null;
  });

  it('chat transport streams events and resolves at termination', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime();
    await rt.start();
    const transport = createChatStreamTransport(rt, {
      sessionId: () => 'sess_1',
      params: { provider: 'mock', model: 'm' },
    });
    const events: SSEEvent[] = [];
    await transport.stream({ content: 'hello' }, (ev) => events.push(ev));
    const text = events
      .filter((e) => e.type === 'content')
      .map((e) => e.content ?? '')
      .join('');
    expect(text).toBe('echo:hello');
    expect(events[events.length - 1].type).toBe('done');
  });

  it('chat transport steer resolves false with no active stream, true mid-stream', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime({ FAKE_HANG_STREAM: '1' });
    await rt.start();
    const transport = createChatStreamTransport(rt, {
      sessionId: () => 'sess_1',
      params: { provider: 'mock', model: 'm' },
    });
    await expect(transport.steer?.('early')).resolves.toBe(false);
    const events: SSEEvent[] = [];
    const streaming = transport.stream({ content: 'hang' }, (ev) => events.push(ev));
    // Give the stream a tick to open, then steer it.
    await new Promise((r) => setTimeout(r, 100));
    await expect(transport.steer?.('nudge')).resolves.toBe(true);
    // The hook contract has no mid-stream cancel handle; hosts cancel via
    // the runtime. Closing the runtime fails the sink and ends the loop.
    await rt.close();
    rt = null;
    await streaming.catch(() => undefined);
  });

  it('session transport maps create/resume/list onto the runtime', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime();
    await rt.start();
    const transport = createSessionTransport(rt);
    const created = await transport.create({ chatId: 'c9', userId: 'u9' });
    expect(created.sessionId).toBe('sess_c9');
    expect((await transport.resume('sess_c9')).sessionId).toBe('sess_c9');
    expect(await transport.list({ userId: 'u9' })).toEqual([]);
  });
});
