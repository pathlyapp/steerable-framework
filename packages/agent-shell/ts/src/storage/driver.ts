import type { ScopedStore } from './scoped-store.js';

/** Ownership context applied to every host and scenario-pack database operation. */
export interface TenantScope {
  tenantId: string;
  userId: string;
}

/** Ownership used by the personal, single-user product. */
export const LOCAL_SCOPE: TenantScope = Object.freeze({
  tenantId: 'local',
  userId: 'local',
});

export type PackDbParams =
  | readonly unknown[]
  | Readonly<Record<string, unknown>>;

export interface PackDbRunResult {
  changes: number;
  lastInsertRowid: number | bigint;
}

/**
 * Driver-neutral SQL access for scenario-pack repositories.
 *
 * The access object carries scope but cannot safely rewrite arbitrary SQL.
 * Every pack statement must include tenant_id and user_id predicates or values;
 * drivers and source gates reject missing or mismatched ownership. The driver
 * connection is never exposed.
 */
export interface PackDbAccess {
  readonly scope: TenantScope;
  get<T extends Record<string, unknown>>(
    sql: string,
    params?: PackDbParams,
  ): Promise<T | undefined>;
  all<T extends Record<string, unknown>>(
    sql: string,
    params?: PackDbParams,
  ): Promise<T[]>;
  run(sql: string, params?: PackDbParams): Promise<PackDbRunResult>;
  exec(sql: string): Promise<void>;
  transaction<T>(operation: (db: PackDbAccess) => Promise<T>): Promise<T>;
}

export type { ScopedStore } from './scoped-store.js';

/** Product-selectable host storage implementation. */
export interface StorageDriver {
  initialize(): Promise<void>;
  scoped(scope: TenantScope): ScopedStore;
  packAccess(scope: TenantScope): PackDbAccess;
  applyPackMigrations(): Promise<void>;
  close(): Promise<void>;
}

export type StorageDriverFactory = () => StorageDriver | Promise<StorageDriver>;

const DEFAULT_DRIVER_ID = 'sqlite';
const factories = new Map<string, StorageDriverFactory>();
let selectedDriverId = DEFAULT_DRIVER_ID;
let activeDriver: StorageDriver | null = null;
let initialization: Promise<StorageDriver> | null = null;

/** Registers a storage driver. Product packs call this during composition. */
export function registerStorageDriver(
  id: string,
  factory: StorageDriverFactory,
  options: { default?: boolean } = {},
): () => void {
  if (factories.has(id)) {
    throw new Error(`[storage] duplicate driver registration: ${id}`);
  }
  if (activeDriver || initialization) {
    throw new Error('[storage] drivers must be registered before initialization');
  }
  factories.set(id, factory);
  if (options.default) selectedDriverId = id;
  return () => {
    if (!activeDriver && !initialization) {
      factories.delete(id);
      if (selectedDriverId === id) selectedDriverId = DEFAULT_DRIVER_ID;
    }
  };
}

/** Selects a registered driver before storage initialization. */
export function selectStorageDriver(id: string): void {
  if (activeDriver || initialization) {
    throw new Error('[storage] driver selection is closed after initialization');
  }
  selectedDriverId = id;
}

/**
 * Initializes storage after all product-pack registrations have run.
 *
 * Calls are idempotent and share one initialization promise.
 */
export function initializeStorage(): Promise<StorageDriver> {
  if (activeDriver) return Promise.resolve(activeDriver);
  if (initialization) return initialization;
  const attempt = (async () => {
    const factory = factories.get(selectedDriverId);
    if (!factory) {
      throw new Error(`[storage] unknown storage driver: ${selectedDriverId}`);
    }
    let driver: StorageDriver | null = null;
    try {
      driver = await factory();
      await driver.initialize();
      await driver.applyPackMigrations();
      activeDriver = driver;
      return driver;
    } catch (error) {
      if (driver) {
        try {
          await driver.close();
        } catch (_closeError) {
          // Initialization error remains the actionable startup failure.
        }
      }
      throw error;
    }
  })();
  initialization = attempt;
  void attempt.catch(() => {
    if (initialization === attempt) initialization = null;
  });
  return attempt;
}

/** Returns a scoped view after explicit storage initialization. */
export function getScopedStore(scope: TenantScope): ScopedStore {
  if (!activeDriver) {
    throw new Error('[storage] initializeStorage() must complete before use');
  }
  return activeDriver.scoped(scope);
}

/** Returns scope-bound scenario-pack access after initialization. */
export function getPackDbAccess(scope: TenantScope): PackDbAccess {
  if (!activeDriver) {
    throw new Error('[storage] initializeStorage() must complete before use');
  }
  return activeDriver.packAccess(scope);
}

/** Closes the active driver. Mainly used by host shutdown and tests. */
export async function closeStorage(): Promise<void> {
  const driver = activeDriver;
  activeDriver = null;
  initialization = null;
  if (driver) await driver.close();
}

registerStorageDriver(DEFAULT_DRIVER_ID, async () => {
  const { SqliteStorageDriver } = await import('./sqlite-driver.js');
  return new SqliteStorageDriver();
});
