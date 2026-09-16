/**
 * The `localStore` singleton's friendly handling of a held write lease.
 *
 * `createLocalStore` turns a `StoreAlreadyOwnedError` into a readable dialog
 * + exit instead of a raw stack. The single-instance lock (single-instance.ts)
 * already stops a second OS process earlier; this is the deeper kernel-lock
 * backstop for the cases it cannot reach (a bypassed lock, two userData dirs
 * pointed at one database, a crashed-then-relaunched app racing its own
 * cleanup).
 *
 * This lives in its own module — and takes an injectable constructor — so the
 * error-mapping path is testable in a plain-node vitest worker. Importing
 * `storage/index.ts` constructs the real `LocalStore`, which loads
 * better-sqlite3 (Electron ABI) and cannot resolve outside Electron; this
 * module imports only the error class, so it loads anywhere. `dialog`/`app`
 * are loaded lazily for the same reason.
 */

import { StoreAlreadyOwnedError } from './write-lease.js';

/** The slice of electron's app/dialog the friendly exit uses. */
interface ElectronExit {
  app: { exit(code: number): void };
  dialog: { showErrorBox(title: string, message: string): void };
}

export function createLocalStore<T>(
  construct: () => T,
  loadElectron: () => Promise<ElectronExit> = () =>
    import('electron') as unknown as Promise<ElectronExit>,
  exitProcess: (code: number) => void = (code) => process.exit(code),
): T {
  try {
    return construct();
  } catch (err) {
    if (err instanceof StoreAlreadyOwnedError) {
      void loadElectron()
        .then(({ app, dialog }) => {
          dialog.showErrorBox(
            '无法启动',
            '另一个实例正在运行并占用本地数据库。请先关闭该实例，再重新打开应用。',
          );
          app.exit(1);
        })
        .catch(() => exitProcess(1));
      // Unreachable once electron resolves (app.exit ends the process); the
      // rethrow is the non-electron fallback so a plain-node import still
      // fails loud rather than returning a half-built store.
      throw err;
    }
    throw err;
  }
}
