/**
 * Friendly handling of a held write lease.
 *
 * `acquireWriteLeaseOrExit` takes injectable acquisition, electron loader, and
 * exit precisely so this path is testable in a plain-node vitest worker: the
 * real `new LocalStore()` loads better-sqlite3 (Electron ABI) and `electron`
 * does not resolve here. The behavior under test is the error mapping — a
 * `StoreAlreadyOwnedError` becomes a readable dialog + exit, any other error
 * propagates unchanged.
 */

import { describe, expect, it, vi } from 'vitest';
import { acquireWriteLeaseOrExit } from '../../src/storage/write-lease-error.js';
import { StoreAlreadyOwnedError } from '../../src/storage/write-lease.js';

describe('acquireWriteLeaseOrExit', () => {
  it('returns the constructed store on success', () => {
    const sentinel = { marker: true };
    const store = acquireWriteLeaseOrExit(() => sentinel);
    expect(store).toBe(sentinel);
  });

  it('rethrows a non-lease error unchanged', () => {
    const boom = new Error('corrupt schema');
    expect(() =>
      acquireWriteLeaseOrExit(() => {
        throw boom;
      }),
    ).toThrow(boom);
  });

  it('shows a readable dialog and exits on StoreAlreadyOwnedError', async () => {
    const showErrorBox = vi.fn();
    const appExit = vi.fn();
    const loadElectron = vi.fn().mockResolvedValue({
      app: { exit: appExit },
      dialog: { showErrorBox },
    });
    expect(() =>
      acquireWriteLeaseOrExit(
        () => {
          throw new StoreAlreadyOwnedError('/tmp/x.lock');
        },
        loadElectron,
      ),
    ).toThrow(StoreAlreadyOwnedError);
    // The dialog + exit fire on the microtask after the throw.
    await vi.waitFor(() => {
      expect(showErrorBox).toHaveBeenCalledWith(
        '无法启动',
        expect.stringContaining('另一个实例正在运行'),
      );
      expect(appExit).toHaveBeenCalledWith(1);
    });
  });

  it('falls back to process.exit when electron does not resolve', async () => {
    const exitProcess = vi.fn();
    const loadElectron = vi.fn().mockRejectedValue(new Error('not electron'));
    expect(() =>
      acquireWriteLeaseOrExit(
        () => {
          throw new StoreAlreadyOwnedError('/tmp/x.lock');
        },
        loadElectron,
        exitProcess,
      ),
    ).toThrow(StoreAlreadyOwnedError);
    await vi.waitFor(() => {
      expect(exitProcess).toHaveBeenCalledWith(1);
    });
  });
});
