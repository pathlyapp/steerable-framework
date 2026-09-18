import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  closeStorage,
  initializeStorage,
  registerStorageDriver,
  type StorageDriver,
} from '../../src/storage/driver.js';
import type { ScopedStore } from '../../src/storage/scoped-store.js';

afterEach(async () => {
  await closeStorage();
});

describe('storage driver registry', () => {
  it('closes a failed driver and permits initialization retry', async () => {
    const failedClose = vi.fn(async () => undefined);
    const successfulClose = vi.fn(async () => undefined);
    let attempts = 0;
    const unregister = registerStorageDriver('retry-test', () => {
      attempts += 1;
      const fails = attempts === 1;
      return {
        initialize: async () => {
          if (fails) throw new Error('initialization failed');
        },
        scoped: () => null as unknown as ScopedStore,
        packAccess: () => {
          throw new Error('not used');
        },
        applyPackMigrations: async () => undefined,
        close: fails ? failedClose : successfulClose,
      } satisfies StorageDriver;
    }, { default: true });

    await expect(initializeStorage()).rejects.toThrow('initialization failed');
    expect(failedClose).toHaveBeenCalledOnce();
    await expect(initializeStorage()).resolves.toBeDefined();
    expect(attempts).toBe(2);

    await closeStorage();
    expect(successfulClose).toHaveBeenCalledOnce();
    unregister();
  });
});
