/**
 * SQL that lists chat sessions with no rows in `chat_messages`.
 *
 * Bind `exceptChatId` twice. Pass `null` for both placeholders to include
 * every empty session; a concrete id keeps that session (the one currently
 * open in the composer) so a first-send race cannot delete it before the
 * user message lands.
 */
export const LIST_EMPTY_CHAT_IDS_SQL = `
  SELECT id
  FROM chat_sessions
  WHERE (? IS NULL OR id != ?)
    AND NOT EXISTS (
      SELECT 1 FROM chat_messages WHERE chat_id = chat_sessions.id
    )
`;
