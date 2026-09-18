import path from 'node:path';
import Database from 'better-sqlite3';

import { getProductConfig } from '../product-config.js';
import { getUserDataDir } from '../runtime.js';
import type {
  PackDbAccess,
  PackDbParams,
  PackDbRunResult,
  StorageDriver,
  TenantScope,
} from './driver.js';
import { LOCAL_SCOPE } from './driver.js';
import { SqliteScopedStore } from './index.js';
import type { ScopedStore } from './scoped-store.js';
import {
  acquireWriteLease,
  lockPathForDb,
  type HeldWriteLease,
} from './write-lease.js';
import { acquireWriteLeaseOrExit } from './write-lease-error.js';

function invokeStatement(
  statement: Database.Statement,
  operation: 'get' | 'all' | 'run',
  params?: PackDbParams,
): unknown {
  if (Array.isArray(params)) return statement[operation](...params);
  if (params) return statement[operation](params);
  return statement[operation]();
}

function assertScopedPackStatement(
  sql: string,
  params: PackDbParams | undefined,
  scope: TenantScope,
): void {
  if (/^\s*PRAGMA\b/i.test(sql) || /\bsqlite_master\b/i.test(sql)) return;
  if (!/\btenant_id\b/i.test(sql) || !/\buser_id\b/i.test(sql)) {
    throw new Error('[storage] pack SQL must bind tenant_id and user_id');
  }
  if (!params) {
    throw new Error('[storage] scoped pack parameters are required');
  }
  if (Array.isArray(params)) {
    if (!params.includes(scope.tenantId) || !params.includes(scope.userId)) {
      throw new Error('[storage] pack parameters do not match the bound scope');
    }
    return;
  }
  const named = params as Readonly<Record<string, unknown>>;
  const tenantId = named.tenantId ?? named.tenant_id;
  const userId = named.userId ?? named.user_id;
  if (tenantId !== scope.tenantId || userId !== scope.userId) {
    throw new Error('[storage] pack parameters do not match the bound scope');
  }
}

class SqlitePackDbAccess implements PackDbAccess {
  constructor(
    private readonly db: Database.Database,
    readonly scope: TenantScope,
  ) {}

  async get<T extends Record<string, unknown>>(
    sql: string,
    params?: PackDbParams,
  ): Promise<T | undefined> {
    assertScopedPackStatement(sql, params, this.scope);
    return invokeStatement(this.db.prepare(sql), 'get', params) as T | undefined;
  }

  async all<T extends Record<string, unknown>>(
    sql: string,
    params?: PackDbParams,
  ): Promise<T[]> {
    assertScopedPackStatement(sql, params, this.scope);
    return invokeStatement(this.db.prepare(sql), 'all', params) as T[];
  }

  async run(sql: string, params?: PackDbParams): Promise<PackDbRunResult> {
    assertScopedPackStatement(sql, params, this.scope);
    const result = invokeStatement(this.db.prepare(sql), 'run', params) as Database.RunResult;
    return { changes: result.changes, lastInsertRowid: result.lastInsertRowid };
  }

  async exec(sql: string): Promise<void> {
    this.db.exec(sql);
  }

  async transaction<T>(operation: (db: PackDbAccess) => Promise<T>): Promise<T> {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const value = await operation(this);
      this.db.exec('COMMIT');
      return value;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }
}

/** Default local SQLite storage driver. */
export class SqliteStorageDriver implements StorageDriver {
  private db: Database.Database | null = null;
  private writeLease: HeldWriteLease | null = null;
  private localSqliteStore: SqliteScopedStore | null = null;
  private readonly stores = new Map<string, ScopedStore>();

  async initialize(): Promise<void> {
    if (this.db) return;
    const dbPath = path.join(
      getUserDataDir(),
      getProductConfig().dbFileName ?? 'agent-shell.db',
    );
    this.writeLease = acquireWriteLeaseOrExit(() =>
      acquireWriteLease(lockPathForDb(dbPath)),
    );
    let db: Database.Database | null = null;
    try {
      db = new Database(dbPath);
      db.pragma('journal_mode = WAL');
      db.pragma('foreign_keys = ON');
      this.db = db;
      const local = new SqliteScopedStore(db, LOCAL_SCOPE);
      await local.initialize();
      this.localSqliteStore = local;
      this.stores.set(this.scopeKey(LOCAL_SCOPE), local);
    } catch (error) {
      db?.close();
      this.writeLease.release();
      this.writeLease = null;
      throw error;
    }
  }

  scoped(scope: TenantScope): ScopedStore {
    const db = this.requireDb();
    const key = this.scopeKey(scope);
    const existing = this.stores.get(key);
    if (existing) return existing;
    const store = new SqliteScopedStore(db, scope);
    this.stores.set(key, store);
    return store;
  }

  packAccess(scope: TenantScope): PackDbAccess {
    return new SqlitePackDbAccess(this.requireDb(), scope);
  }

  async applyPackMigrations(): Promise<void> {
    this.localSqliteStore?.applyPackMigrations();
  }

  async close(): Promise<void> {
    this.stores.clear();
    this.db?.close();
    this.db = null;
    this.localSqliteStore = null;
    this.writeLease?.release();
    this.writeLease = null;
  }

  private requireDb(): Database.Database {
    if (!this.db) throw new Error('[storage] SQLite driver is not initialized');
    return this.db;
  }

  private scopeKey(scope: TenantScope): string {
    return `${scope.tenantId}\u0000${scope.userId}`;
  }
}
