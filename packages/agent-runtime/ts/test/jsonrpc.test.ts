import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

const PROC_TEST_TIMEOUT = 20_000;
import {
  JsonRpcPeer,
  JsonRpcRemoteError,
  JsonRpcTransportClosedError,
} from '../src/jsonrpc.js';

const FAKE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fake-sidecar.mjs');

interface Harness {
  peer: JsonRpcPeer;
  notifications: Array<{ method: string; params: unknown }>;
  kill: () => void;
}

function harness(env: NodeJS.ProcessEnv = {}, onRequest?: (m: string, p: unknown) => Promise<unknown>): Harness {
  const child = spawn(process.execPath, [FAKE], {
    env: { ...process.env, ...env },
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const notifications: Array<{ method: string; params: unknown }> = [];
  const peer = new JsonRpcPeer({
    write: (line) => child.stdin.write(line),
    onNotification: (method, params) => notifications.push({ method, params }),
    onRequest,
  });
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk: string) => peer.feed(chunk));
  return { peer, notifications, kill: () => child.kill('SIGKILL') };
}

describe('JsonRpcPeer', () => {
  let h: Harness | null = null;
  afterEach(() => {
    h?.kill();
    h = null;
  });

  it('resolves a request with its result', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness();
    const health = await h.peer.request<{ status: string }>('system.ping');
    expect(health.status).toBe('ok');
  });

  it('rejects with JsonRpcRemoteError on remote errors', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness();
    await expect(h.peer.request('no.such.method')).rejects.toMatchObject({
      name: 'JsonRpcRemoteError',
      code: -32601,
    });
  });

  it('dispatches notifications', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness();
    await h.peer.request('agent.chat.stream', { message: 'hi' });
    await new Promise((r) => setTimeout(r, 100));
    const methods = h!.notifications.map((n) => n.method);
    expect(methods).toContain('lifecycle.ready');
    expect(methods).toContain('stream.chunk');
    expect(methods).toContain('stream.done');
  });

  it('answers reverse-channel requests via onRequest', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness({ FAKE_REVERSE: '1' }, async (method, params) => {
      expect(method).toBe('tool.invoke');
      const call = params as { name: string; arguments: { text: string } };
      return { id: 'tc_1', name: call.name, success: true, result: call.arguments.text };
    });
    await h.peer.request('agent.chat.stream', { message: 'hi' });
    await new Promise((r) => setTimeout(r, 150));
    const chunk = h!.notifications.find(
      (n) => n.method === 'stream.chunk' && String((n.params as { delta?: string }).delta).startsWith('reverse:'),
    );
    expect(chunk).toBeDefined();
    expect((chunk!.params as { delta: string }).delta).toContain('hello');
  });

  it('answers -32601 for reverse requests with no handler', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness({ FAKE_REVERSE: '1' });
    await h.peer.request('agent.chat.stream', { message: 'hi' });
    await new Promise((r) => setTimeout(r, 150));
    const chunk = h!.notifications.find(
      (n) => n.method === 'stream.chunk' && String((n.params as { delta?: string }).delta).startsWith('reverse:'),
    );
    expect(chunk).toBeDefined();
    expect((chunk!.params as { delta: string }).delta).toContain('-32601');
  });

  it('rejects a timed-out request and still resolves the next one', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness();
    await expect(
      h.peer.request('test.blackhole', undefined, { timeoutMs: 50 }),
    ).rejects.toMatchObject({ name: 'JsonRpcTransportClosedError' });
    const pong = await h.peer.request<{ status: string }>('system.ping');
    expect(pong.status).toBe('ok');
  });

  it('rejects pending and future requests after close()', { timeout: PROC_TEST_TIMEOUT }, async () => {
    h = harness();
    h.peer.close('test close');
    await expect(h.peer.request('system.ping')).rejects.toBeInstanceOf(
      JsonRpcTransportClosedError,
    );
  });

  it('survives partial frames split across chunks', () => {
    const seen: unknown[] = [];
    const peer = new JsonRpcPeer({
      write: () => undefined,
      onNotification: (method, params) => seen.push([method, params]),
    });
    const frame = JSON.stringify({ jsonrpc: '2.0', method: 'stream.chunk', params: { streamId: 's', delta: 'x' } });
    peer.feed(frame.slice(0, 10));
    expect(seen).toHaveLength(0);
    peer.feed(frame.slice(10) + '\n');
    expect(seen).toHaveLength(1);
    peer.close();
  });
});
