import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

const PROC_TEST_TIMEOUT = 20_000;
import { AgentRuntime } from '../src/runtime.js';

const FAKE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fake-sidecar.mjs');

function runtime(env: NodeJS.ProcessEnv = {}, tools = {}) {
  return new AgentRuntime({
    python: process.execPath,
    entryModule: null,
    args: [FAKE],
    env,
    readyTimeoutMs: 8_000,
    shutdownGraceMs: 1_000,
    restart: false,
    tools,
  });
}

describe('AgentRuntime', () => {
  let rt: AgentRuntime | null = null;
  afterEach(async () => {
    await rt?.close();
    rt = null;
  });

  it('streams a chat turn as an async iterable with a terminal status', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime();
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'hello' }] });
    const types: string[] = [];
    let text = '';
    for await (const ev of handle.events) {
      types.push(ev.type);
      if (ev.type === 'content') text += ev.content ?? '';
    }
    expect(text).toBe('echo:hello');
    expect(types).toContain('content');
    expect(types[types.length - 1]).toBe('done');
    await expect(handle.done).resolves.toEqual({ status: 'completed', cancelled: false });
  });

  it('survives response+chunks+done coalescing into a single read', { timeout: PROC_TEST_TIMEOUT }, async () => {
    // Regression: stream.done used to delete the sink before chatStream's
    // continuation looked it up — the consumer then hung on an empty sink.
    rt = runtime({ FAKE_SYNC_STREAM: '1' });
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'one-write' }] });
    let text = '';
    for await (const ev of handle.events) {
      if (ev.type === 'content') text += ev.content ?? '';
    }
    expect(text).toBe('echo:one-write');
    await expect(handle.done).resolves.toEqual({ status: 'completed', cancelled: false });
  });

  it('surfaces stream.error as an error event and error status', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime();
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'fail' }] });
    const types: string[] = [];
    for await (const ev of handle.events) types.push(ev.type);
    expect(types).toContain('error');
    await expect(handle.done).resolves.toEqual({ status: 'error', cancelled: false });
  });

  it('cancelChat lands a cancelled terminal status', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime({ FAKE_HANG_STREAM: '1' });
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'hang' }] });
    await rt.cancelChat(handle.streamId);
    await expect(handle.done).resolves.toEqual({ status: 'cancelled', cancelled: true });
  });

  it('steerChat targets the active stream', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime({ FAKE_HANG_STREAM: '1' });
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'hang' }] });
    await expect(rt.steerChat(handle.streamId, 'nudge')).resolves.toBe(true);
    await rt.cancelChat(handle.streamId);
    await handle.done;
  });

  it('routes reverse tool.invoke calls to the registered host handler', { timeout: PROC_TEST_TIMEOUT }, async () => {
    const seen: unknown[] = [];
    rt = runtime({ FAKE_REVERSE: '1' }, {
      echo: async (args: Record<string, unknown>) => {
        seen.push(args);
        return { success: true, result: `host:${String(args.text)}` };
      },
    });
    await rt.start();
    const handle = await rt.chatStream({ provider: 'mock', model: 'm', messages: [{ role: 'user', content: 'hi' }] });
    let text = '';
    for await (const ev of handle.events) {
      if (ev.type === 'content') text += ev.content ?? '';
    }
    expect(seen).toEqual([{ text: 'hello' }]);
    expect(text).toContain('host:hello');
    expect(text).toContain('msg:hi');
  });

  it('covers the session / tool / skills / workspace / trace / config surface', { timeout: PROC_TEST_TIMEOUT }, async () => {
    rt = runtime();
    await rt.start();
    const created = await rt.createSession({ chatId: 'c1', userId: 'u1' });
    expect(created.sessionId).toBe('sess_c1');
    expect((await rt.resumeSession('sess_c1')).sessionId).toBe('sess_c1');
    expect(await rt.listSessions({ userId: 'u1' })).toEqual([]);
    expect((await rt.forkSession({ sessionId: 'sess_c1', recordId: 'r' })).lineage).toBe('L');
    expect((await rt.sessionBranches('L')).children).toEqual([]);
    expect((await rt.forkChat('s_x', 'lbl')).seq).toBe(2);
    expect((await rt.listTools())[0].name).toBe('echo');
    expect((await rt.invokeTool({ id: 't', name: 'echo', arguments: {} } as never)).success).toBe(true);
    expect((await rt.listSkills(['/x'])).skills[0].name).toBe('s');
    expect((await rt.applyEdits({ content: 'a', edits: [] })).applied).toBe(1);
    expect((await rt.fetchTrace('t1')).trace).toMatchObject({ traceId: 't1' });
    expect((await rt.exportTrace('t1')).status).toBe('exported');
    expect(await rt.getConfig()).toMatchObject({ model: 'fake' });
    await rt.setConfig({ model: 'other' });
  });
});
