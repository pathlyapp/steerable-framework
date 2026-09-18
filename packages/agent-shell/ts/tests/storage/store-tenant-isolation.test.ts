import Database from 'better-sqlite3';
import { afterEach, describe, expect, it } from 'vitest';

import { SqliteScopedStore } from '../../src/storage/index.js';

const databases: Database.Database[] = [];

async function stores() {
  const db = new Database(':memory:');
  db.pragma('foreign_keys = ON');
  databases.push(db);
  const first = new SqliteScopedStore(db, { tenantId: 'tenant-a', userId: 'user-a' });
  await first.initialize();
  const second = new SqliteScopedStore(db, { tenantId: 'tenant-b', userId: 'user-b' });
  return { first, second };
}

afterEach(() => {
  for (const db of databases.splice(0)) db.close();
});

describe('SqliteScopedStore tenant isolation', () => {
  it('isolates chat, agent, settings, usage, and insight lists', async () => {
    const { first, second } = await stores();
    await first.createChatWithId('shared-chat', 'first');
    await second.createChatWithId('shared-chat', 'second');
    await first.createChatAgent({ id: 'private-agent', name: 'private' });
    await first.setLlmSettings({ provider: 'ollama', model: 'private-model' });
    await first.recordUsageEvent({
      kind: 'chat',
      provider: 'ollama',
      model: 'private-model',
      promptTokens: 1,
      completionTokens: 2,
      totalTokens: 3,
    });
    await first.enqueueInsight('event', { private: true });

    expect((await first.getChat('shared-chat'))?.title).toBe('first');
    expect((await second.getChat('shared-chat'))?.title).toBe('second');
    expect(await second.getChatAgent('private-agent')).toBeNull();
    expect(await second.getLlmSettings()).toBeNull();
    expect((await second.getUsageSummary()).totals.totalTokens).toBe(0);
    expect(await second.listInsightOutbox()).toEqual([]);
  });

  it('rejects cross-tenant trace and task id lookups and unqualified task lists', async () => {
    const { first, second } = await stores();
    await first.createChatWithId('chat-a');
    await second.createChatWithId('chat-b');
    const task = await first.createTask({ chatId: 'chat-a', task: 'private task' });
    await first.saveTrace({
      id: 'private-trace',
      chatId: 'chat-a',
      startedAtMs: 1,
      status: 'completed',
      payload: {},
    });

    expect(await second.getTask(task.id)).toBeNull();
    expect(await second.listTasks()).toEqual([]);
    expect(await second.getTrace('private-trace')).toBeNull();
    expect(await second.listTracesByChat('chat-a')).toEqual([]);
    expect(await second.updateTask(task.id, { status: 'failed' })).toBeNull();
  });
});
