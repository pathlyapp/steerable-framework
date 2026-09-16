/**
 * Cross-process write lease for one sqlite database.
 *
 * Sibling lock file: `<dbFileName>` → 同路径换 `.lock` 后缀。
 *
 * The lease is an open `BEGIN EXCLUSIVE` transaction on the lock file,
 * which is a kernel lock in SQLite's VFS (fcntl on POSIX, LockFileEx on
 * Windows). That gives the three properties the desktop needs: a second
 * process fails loud (`StoreAlreadyOwnedError`) instead of corrupting a
 * shared WAL, process death releases the lock without a TTL, and no
 * caller can steal a live lease.
 *
 * Node exposes no portable `flock`, and `fs.constants.O_EXLOCK` does not
 * exist on any platform, so SQLite's own locking is the only kernel lock
 * reachable from here — and it is already a shipped dependency because
 * the database it guards is sqlite.
 */

import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

export class StoreAlreadyOwnedError extends Error {
  readonly lockPath: string;

  constructor(lockPath: string) {
    super(
      `store already owned: ${lockPath} (another process has this database open for write)`,
    );
    this.name = 'StoreAlreadyOwnedError';
    this.lockPath = lockPath;
  }
}

export interface HeldWriteLease {
  release(): void;
}

/** SQLite reports a live incumbent as SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT. */
function isSqliteBusy(err: unknown): boolean {
  const code = (err as { code?: unknown } | null)?.code;
  return typeof code === 'string' && code.startsWith('SQLITE_BUSY');
}

export function lockPathForDb(dbPath: string): string {
  const parsed = path.parse(path.resolve(dbPath));
  return path.join(parsed.dir, `${parsed.name}.lock`);
}

export function acquireWriteLease(lockPath: string): HeldWriteLease {
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  // Loaded lazily so tests that only exercise lockPathForDb do not need
  // better-sqlite3's Electron ABI.
  const Database = require('better-sqlite3') as typeof import('better-sqlite3');
  const db = new Database(lockPath, { timeout: 0 });
  try {
    // Every statement here can hit the incumbent's lock, including the
    // journal_mode pragma — it rewrites the header and so needs the same
    // write lock. busy_timeout 0 makes that immediate instead of a stall.
    db.pragma('busy_timeout = 0');
    db.pragma('journal_mode = DELETE');
    db.exec('CREATE TABLE IF NOT EXISTS lease (id INTEGER PRIMARY KEY)');
    db.exec('BEGIN EXCLUSIVE');
  } catch (err) {
    db.close();
    // Only contention becomes StoreAlreadyOwnedError; a corrupt or
    // unreadable lock file must not masquerade as a second instance.
    if (isSqliteBusy(err)) {
      throw new StoreAlreadyOwnedError(lockPath);
    }
    throw err;
  }
  let released = false;
  return {
    release() {
      if (released) return;
      released = true;
      // Closing rolls the transaction back and drops the kernel lock. The
      // lock file itself is never deleted.
      db.close();
    },
  };
}
