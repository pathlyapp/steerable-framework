import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import Database from 'better-sqlite3';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LOCAL_SCOPE } from '../../src/storage/driver.js';
import { registerPackMigrations } from '../../src/storage/pack-migrations.js';
import { registerPackAgentSeeds } from '../../src/storage/pack-seeds.js';
import { SqliteStorageDriver } from '../../src/storage/sqlite-driver.js';
import { StoreAlreadyOwnedError } from '../../src/storage/write-lease.js';

const handles: Array<{ driver: SqliteStorageDriver; dir: string }> = [];

async function createDriver(dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sqlite-driver-'))) {
  const previous = process.env.DEEPPATH_USER_DATA_DIR;
  process.env.DEEPPATH_USER_DATA_DIR = dir;
  const driver = new SqliteStorageDriver();
  try {
    await driver.initialize();
  } finally {
    if (previous === undefined) delete process.env.DEEPPATH_USER_DATA_DIR;
    else process.env.DEEPPATH_USER_DATA_DIR = previous;
  }
  handles.push({ driver, dir });
  return { driver, store: driver.scoped(LOCAL_SCOPE), dir };
}

afterEach(async () => {
  for (const { driver, dir } of handles.splice(0)) {
    await driver.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe('SqliteStorageDriver lifecycle', () => {
  it('seeds an active scenario-pack agent in the local scope', async () => {
    registerPackAgentSeeds('lifecycle-seed', [{
      id: 'lifecycle-seed-agent',
      slug: 'lifecycle-seed-agent',
      name: 'Lifecycle seed',
      rolePrompt: 'Test the scoped seed insert.',
    }], true);
    const { store } = await createDriver();
    expect(await store.getChatAgent('lifecycle-seed-agent')).toMatchObject({
      id: 'lifecycle-seed-agent',
      name: 'Lifecycle seed',
    });
  });

  it('opens lazily with WAL, foreign keys, schema, and write lease', async () => {
    const { driver, dir } = await createDriver();
    expect(fs.existsSync(path.join(dir, 'agent-shell.db'))).toBe(true);
    expect(fs.existsSync(path.join(dir, 'agent-shell.lock'))).toBe(true);
    const db = driver.packAccess(LOCAL_SCOPE);
    expect(await db.all<{ journal_mode: string }>('PRAGMA journal_mode')).toEqual([
      { journal_mode: 'wal' },
    ]);
    expect(await db.all<{ foreign_keys: number }>('PRAGMA foreign_keys')).toEqual([
      { foreign_keys: 1 },
    ]);
    const rows = await db.all<{ name: string }>(
      `SELECT name FROM sqlite_master WHERE type = 'table'`,
    );
    const names = new Set(rows.map((row) => row.name));
    for (const table of [
      'chat_sessions',
      'chat_messages',
      'chat_agents',
      'settings_kv',
      'harness_traces',
      'usage_events',
      'insights_outbox',
      'tasks',
    ]) {
      expect(names).toContain(table);
    }
  });

  it('rejects a second driver for the same database', async () => {
    const first = await createDriver();
    const previous = process.env.DEEPPATH_USER_DATA_DIR;
    process.env.DEEPPATH_USER_DATA_DIR = first.dir;
    const second = new SqliteStorageDriver();
    const exit = vi.spyOn(process, 'exit').mockImplementation((() => undefined) as never);
    try {
      await expect(second.initialize()).rejects.toBeInstanceOf(StoreAlreadyOwnedError);
      await vi.waitFor(() => expect(exit).toHaveBeenCalledWith(1));
    } finally {
      exit.mockRestore();
      if (previous === undefined) delete process.env.DEEPPATH_USER_DATA_DIR;
      else process.env.DEEPPATH_USER_DATA_DIR = previous;
    }
    expect((await first.store.createChat('still alive')).title).toBe('still alive');
  });

  it('keeps independent database directories isolated', async () => {
    const a = await createDriver();
    const b = await createDriver();
    await a.store.createChat('only A');
    expect((await a.store.listChats()).total).toBe(1);
    expect((await b.store.listChats()).total).toBe(0);
  });

  it('rejects unscoped or mismatched scenario-pack SQL', async () => {
    const { driver } = await createDriver();
    const access = driver.packAccess(LOCAL_SCOPE);
    await expect(access.all('SELECT * FROM chat_sessions')).rejects.toThrow(
      'must bind tenant_id and user_id',
    );
    await expect(access.all(
      'SELECT * FROM chat_sessions WHERE tenant_id = ? AND user_id = ?',
      ['other-tenant', 'other-user'],
    )).rejects.toThrow('do not match the bound scope');
  });

  it('refuses to downgrade a newer scenario-pack schema', async () => {
    const { driver, dir } = await createDriver();
    registerPackMigrations('future-schema-test', { version: 1 });
    const db = new Database(path.join(dir, 'agent-shell.db'));
    db.exec(`
      CREATE TABLE IF NOT EXISTS storage_pack_migrations (
        pack_id TEXT PRIMARY KEY,
        version INTEGER NOT NULL
      );
      INSERT INTO storage_pack_migrations (pack_id, version)
      VALUES ('future-schema-test', 2);
    `);
    db.close();

    await expect(driver.applyPackMigrations()).rejects.toThrow(
      'schema 2 is newer than supported 1',
    );
  });

  it('rebuilds legacy primary keys and preserves rows as local ownership', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sqlite-driver-legacy-'));
    const db = new Database(path.join(dir, 'agent-shell.db'));
    db.exec(`
      CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      INSERT INTO settings_kv (key, value) VALUES ('legacy-key', '"kept"');
    `);
    db.close();

    const { driver } = await createDriver(dir);
    const access = driver.packAccess(LOCAL_SCOPE);
    expect(
      await access.get<{ tenant_id: string; user_id: string; value: string }>(
        `SELECT tenant_id, user_id, value FROM settings_kv
         WHERE tenant_id = @tenantId AND user_id = @userId AND key = 'legacy-key'`,
        { tenantId: 'local', userId: 'local' },
      ),
    ).toEqual({ tenant_id: 'local', user_id: 'local', value: '"kept"' });
    expect(
      (await access.all<{ name: string; pk: number }>('PRAGMA table_info(settings_kv)'))
        .filter((column) => column.pk > 0)
        .map((column) => column.name),
    ).toEqual(['tenant_id', 'user_id', 'key']);
  });
});
