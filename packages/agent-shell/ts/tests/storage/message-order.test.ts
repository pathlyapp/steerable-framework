import { DatabaseSync } from 'node:sqlite';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  MESSAGE_CUT_FROM_ID,
  MESSAGE_ORDER_DESC,
} from '../../src/storage/message-order';

// LocalStore loads better-sqlite3 at module top level, and that native module is
// built against Electron's ABI — importing it here fails before any test runs.
// So the ordering SQL is exported as text and executed here against the real
// chat_messages columns through Node's own SQLite. The SQL under test is the
// same string production runs, not a transcription of it.
const SCHEMA = `
  CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    message_metadata TEXT,
    created_at TEXT NOT NULL
  );
`;

let db: DatabaseSync;

/** Inserts in argument order, so rowid order is insertion order. */
function seed(rows: Array<{ id: string; at: string; chat?: string }>): void {
  const insert = db.prepare(
    `INSERT INTO chat_messages (id, chat_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)`,
  );
  for (const r of rows) insert.run(r.id, r.chat ?? 'c1', r.id, r.at);
}

/** Transcript order as the UI builds it: read newest-first, then reverse. */
function transcript(chat = 'c1'): string[] {
  const rows = db
    .prepare(
      `SELECT id FROM chat_messages WHERE chat_id = ? ORDER BY ${MESSAGE_ORDER_DESC}`,
    )
    .all(chat) as Array<{ id: string }>;
  return rows.map((r) => r.id).reverse();
}

beforeEach(() => {
  db = new DatabaseSync(':memory:');
  db.exec(SCHEMA);
});

describe('chat message ordering', () => {
  it('keeps sub-second writes in the order they happened', () => {
    // The mid-turn steer case: the injected question lands 423ms after the
    // message it interrupted, and its answer only at turn end. Under
    // datetime(created_at) the first two collapsed into one second and the
    // steer rendered above the question it interrupted.
    seed([
      { id: 'asked', at: '2026-09-04T09:39:26.316Z' },
      { id: 'steered', at: '2026-09-04T09:39:26.739Z' },
      { id: 'answer', at: '2026-09-04T09:39:58.165Z' },
    ]);
    expect(transcript()).toEqual(['asked', 'steered', 'answer']);
  });

  it('preserves a whole branch projection written inside one second', () => {
    // replaceChatMessages stamps base + i milliseconds for the entire
    // projection, so every message of a switched branch shares one second.
    const base = Date.parse('2026-09-04T09:39:26.000Z');
    const ids = Array.from({ length: 12 }, (_, i) => `m${String(i).padStart(2, '0')}`);
    seed(ids.map((id, i) => ({ id, at: new Date(base + i).toISOString() })));
    expect(transcript()).toEqual(ids);
  });

  it('falls back to insertion order when timestamps are identical', () => {
    seed([
      { id: 'first', at: '2026-09-04T09:39:26.316Z' },
      { id: 'second', at: '2026-09-04T09:39:26.316Z' },
    ]);
    expect(transcript()).toEqual(['first', 'second']);
  });

  it('orders across a date boundary by time, not by digit width', () => {
    seed([
      { id: 'before', at: '2026-09-04T23:59:59.999Z' },
      { id: 'after', at: '2026-09-05T00:00:00.001Z' },
    ]);
    expect(transcript()).toEqual(['before', 'after']);
  });
});

describe('regenerate cut', () => {
  function cutFrom(messageId: string, chat = 'c1'): number {
    return db
      .prepare(
        `DELETE FROM chat_messages WHERE chat_id = ? AND ${MESSAGE_CUT_FROM_ID}`,
      )
      .run(chat, messageId, chat).changes as number;
  }

  it('removes the target and everything after it', () => {
    seed([
      { id: 'q1', at: '2026-09-04T09:39:26.316Z' },
      { id: 'a1', at: '2026-09-04T09:39:30.000Z' },
      { id: 'q2', at: '2026-09-04T09:40:00.000Z' },
      { id: 'a2', at: '2026-09-04T09:40:05.000Z' },
    ]);
    expect(cutFrom('q2')).toBe(2);
    expect(transcript()).toEqual(['q1', 'a1']);
  });

  it('keeps an earlier message that shares the target timestamp', () => {
    // Regenerating the reply to a steer must not swallow the steer itself.
    seed([
      { id: 'steered', at: '2026-09-04T09:39:26.316Z' },
      { id: 'reply', at: '2026-09-04T09:39:26.316Z' },
    ]);
    expect(cutFrom('reply')).toBe(1);
    expect(transcript()).toEqual(['steered']);
  });

  it('never reaches another chat', () => {
    seed([
      { id: 'mine', at: '2026-09-04T09:39:26.316Z' },
      { id: 'theirs', at: '2026-09-04T09:40:00.000Z', chat: 'c2' },
    ]);
    expect(cutFrom('mine')).toBe(1);
    expect(transcript('c2')).toEqual(['theirs']);
  });

  it('deletes nothing for an unknown id', () => {
    seed([{ id: 'q1', at: '2026-09-04T09:39:26.316Z' }]);
    expect(cutFrom('does-not-exist')).toBe(0);
    expect(transcript()).toEqual(['q1']);
  });
});
