#!/usr/bin/env node
/**
 * Fake sidecar for @steerable/agent-runtime tests: speaks the line-delimited
 * JSON-RPC protocol from docs/spec/sidecar.md over stdin/stdout.
 *
 * Behavior knobs (env):
 *   FAKE_NO_READY=1        never emit lifecycle.ready (start must time out)
 *   FAKE_CRASH_MS=n        exit(1) n ms after ready (restart testing)
 *   FAKE_REVERSE=1         during agent.chat.stream, issue a reverse
 *                          `tool.invoke` request and stream its result back
 *   FAKE_HANG_STREAM=1     stream opens but never terminates (cancel target)
 */
import readline from 'node:readline';

const send = (frame) => process.stdout.write(JSON.stringify(frame) + '\n');
const reverseIds = new Map();
let nextReverseId = 1;
let streamSeq = 0;

if (!process.env.FAKE_NO_READY) {
  send({
    jsonrpc: '2.0',
    method: 'lifecycle.ready',
    params: { version: '0.0.0-fake', protocolVersion: '0.1.0', pid: process.pid },
  });
  if (process.env.FAKE_CRASH_MS) {
    setTimeout(() => process.exit(1), Number(process.env.FAKE_CRASH_MS));
  }
}

function answer(id, result) {
  send({ jsonrpc: '2.0', id, result: result ?? null });
}


// The wire contract carries `messages`; `message` is a legacy convenience.
function userText(params) {
  if (typeof params.message === 'string') return params.message;
  const msgs = Array.isArray(params.messages) ? params.messages : [];
  const last = [...msgs].reverse().find((m) => m && m.role === 'user');
  return last && typeof last.content === 'string' ? last.content : '';
}

function answerError(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

function runStream(id, params) {
  const streamId = `s_fake_${++streamSeq}`;
  const chunk = (extra) =>
    send({ jsonrpc: '2.0', method: 'stream.chunk', params: { streamId, ...extra } });

  if (process.env.FAKE_SYNC_STREAM) {
    // Response + chunks + done in ONE stdout write: exercises the host path
    // where every frame coalesces into a single read (happens under load).
    const frames = [
      { jsonrpc: '2.0', id, result: { streamId } },
      { jsonrpc: '2.0', method: 'stream.chunk', params: { streamId, delta: `echo:${userText(params)}` } },
      { jsonrpc: '2.0', method: 'stream.done', params: { streamId, ok: true } },
    ];
    process.stdout.write(frames.map((f) => JSON.stringify(f)).join('\n') + '\n');
    return;
  }

  answer(id, { streamId });

  if (process.env.FAKE_HANG_STREAM) return; // never terminates

  const finish = (note) => {
    if (note) chunk({ delta: note });
    send({
      jsonrpc: '2.0',
      method: 'stream.done',
      params: { streamId, ok: true },
    });
  };

  if (process.env.FAKE_REVERSE) {
    const rid = `srv_${nextReverseId++}`;
    reverseIds.set(rid, (result) => {
      chunk({ delta: `reverse:${JSON.stringify(result)}` });
      chunk({ delta: ` msg:${userText(params)}` });
      finish();
    });
    send({
      jsonrpc: '2.0',
      id: rid,
      method: 'tool.invoke',
      params: { id: 'tc_1', name: 'echo', arguments: { text: 'hello' } },
    });
    return;
  }

  // W2.2.1: issue a reverse `host.process.spawn` and stream the reply back —
  // the host either answers with a HostSpawnResult or rejects (no handler).
  if (process.env.FAKE_SPAWN) {
    const rid = `srv_${nextReverseId++}`;
    reverseIds.set(rid, (result) => {
      chunk({ delta: `spawn:${JSON.stringify(result)}` });
      finish();
    });
    send({
      jsonrpc: '2.0',
      id: rid,
      method: 'host.process.spawn',
      params: {
        command: 'echo hi',
        policy: { writableRoots: [], network: false, allowedHosts: [] },
      },
    });
    return;
  }

  chunk({ delta: `echo:${userText(params)}` });
  if (userText(params) === 'fail') {
    send({
      jsonrpc: '2.0',
      method: 'stream.error',
      params: { streamId, kind: 'provider', message: 'boom' },
    });
    return;
  }
  finish();
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  const frame = JSON.parse(trimmed);

  // Response to one of our reverse requests.
  if (frame.id !== undefined && frame.method === undefined) {
    const cb = reverseIds.get(frame.id);
    if (cb) {
      reverseIds.delete(frame.id);
      cb(frame.error ? { error: frame.error } : frame.result);
    }
    return;
  }

  const { id, method, params = {} } = frame;
  switch (method) {
    case 'system.ping':
      answer(id, {
        status: 'ok',
        version: '0.0.0-fake',
        protocolVersion: '0.1.0',
        pid: process.pid,
        uptimeS: 0,
      });
      return;
    case 'system.shutdown':
      send({ jsonrpc: '2.0', method: 'lifecycle.shutdown', params: { reason: 'normal' } });
      answer(id, null);
      setTimeout(() => process.exit(0), 10);
      return;
    case 'system.shutdown_now':
      answer(id, null);
      process.exit(0);
      return;
    case 'agent.session.create':
      answer(id, {
        sessionId: `sess_${params.chatId}`,
        chatId: params.chatId,
        userId: params.userId,
        projectId: params.projectId ?? null,
        scenario: params.scenario ?? 'default',
        stageData: params.stageData ?? null,
        createdAt: '2026-08-30T00:00:00Z',
        updatedAt: '2026-08-30T00:00:00Z',
        active: true,
      });
      return;
    case 'agent.session.resume':
      answer(id, {
        sessionId: params.sessionId,
        chatId: 'c',
        userId: 'u',
        projectId: null,
        scenario: 'default',
        stageData: null,
        createdAt: '2026-08-30T00:00:00Z',
        updatedAt: '2026-08-30T00:00:00Z',
        active: true,
      });
      return;
    case 'agent.session.list':
      answer(id, []);
      return;
    case 'agent.session.fork':
      answer(id, { recordId: 'r_1', lineage: 'L', seq: 1, label: params.label ?? null });
      return;
    case 'agent.session.branches':
      answer(id, { lineage: params.lineage, children: [] });
      return;
    case 'agent.chat.stream':
      runStream(id, params);
      return;
    case 'agent.chat.cancel':
      send({
        jsonrpc: '2.0',
        method: 'stream.done',
        params: { streamId: params.streamId, ok: true, cancelled: true, status: 'cancelled' },
      });
      answer(id, null);
      return;
    case 'agent.chat.steer':
      answer(id, { accepted: true });
      return;
    case 'agent.chat.fork':
      answer(id, { recordId: 'r_2', lineage: 'L', seq: 2, label: params.label ?? null });
      return;
    case 'tool.list':
      answer(id, [{ name: 'echo', description: 'fake', parameters: {} }]);
      return;
    case 'tool.invoke':
      answer(id, { id: params.id ?? 'tc', name: params.name, success: true, result: 'ok' });
      return;
    case 'workspace.apply_edits':
      answer(id, { content: 'edited', diff: '@@', applied: 1, matches: [] });
      return;
    case 'skills.list':
      answer(id, { skills: [{ name: 's', path: '/x/SKILL.md' }] });
      return;
    case 'trace.fetch':
      answer(id, { trace: { traceId: params.traceId }, spans: [], events: [] });
      return;
    case 'trace.export':
      answer(id, { status: 'exported', traceId: params.traceId, privacyMode: 'off' });
      return;
    case 'config.get':
      answer(id, { model: 'fake' });
      return;
    case 'config.set':
      answer(id, null);
      return;
    case 'test.blackhole':
      return; // never answers — timeout tests
    default:
      answerError(id, -32601, 'Method not found');
  }
});
