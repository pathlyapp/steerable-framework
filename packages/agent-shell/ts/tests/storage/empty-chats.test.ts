import { DatabaseSync } from 'node:sqlite';
import { describe, expect, it } from 'vitest';
import { LIST_EMPTY_CHAT_IDS_SQL } from '../../src/storage/empty-chats';

const SCHEMA = `
  CREATE TABLE chat_sessions (id TEXT PRIMARY KEY);
  CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
  );
`;

function listEmpty(db: DatabaseSync, exceptChatId: string | null): string[] {
  return (db.prepare(LIST_EMPTY_CHAT_IDS_SQL).all(exceptChatId, exceptChatId) as Array<{
    id: string;
  }>).map((row) => row.id);
}

describe('LIST_EMPTY_CHAT_IDS_SQL', () => {
  it('lists sessions with no messages and can keep the open composer', () => {
    const db = new DatabaseSync(':memory:');
    db.exec(SCHEMA);
    db.prepare(`INSERT INTO chat_sessions (id) VALUES (?)`).run('empty-a');
    db.prepare(`INSERT INTO chat_sessions (id) VALUES (?)`).run('empty-b');
    db.prepare(`INSERT INTO chat_sessions (id) VALUES (?)`).run('filled');
    db.prepare(
      `INSERT INTO chat_messages (id, chat_id, role, content, created_at) VALUES (?, ?, 'user', 'hi', '2026-01-01T00:00:00.000Z')`,
    ).run('m1', 'filled');

    expect(listEmpty(db, null).sort()).toEqual(['empty-a', 'empty-b']);
    expect(listEmpty(db, 'empty-a').sort()).toEqual(['empty-b']);
    db.close();
  });
});
