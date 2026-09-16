import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { lockPathForDb } from '../../src/storage/write-lease.js';

// Only the path mapping is unit-testable here: acquiring a lease loads
// better-sqlite3, which this repo builds against the Electron ABI, so a
// plain-node vitest worker cannot open it. Contention and release-on-death
// are proven across two real processes in tests/e2e/write-lease.e2e.test.ts.
describe('lockPathForDb', () => {
  it('maps a database to its sibling lock file', () => {
    expect(lockPathForDb('/tmp/foo/deeppath-agent.db')).toBe(
      path.resolve('/tmp/foo/deeppath-agent.lock'),
    );
  });

  it('keeps the lock beside the database for any name and extension', () => {
    expect(lockPathForDb('/tmp/foo/sessions.sqlite3')).toBe(
      path.resolve('/tmp/foo/sessions.lock'),
    );
  });

  it('resolves a relative database path before deriving the lock', () => {
    expect(lockPathForDb('data/deeppath-agent.db')).toBe(
      path.resolve('data/deeppath-agent.lock'),
    );
  });
});
