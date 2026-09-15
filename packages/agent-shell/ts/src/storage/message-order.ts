/**
 * The single ordering key for a chat's messages, shared by the read and the
 * regenerate cut so "after" means the same thing to both.
 *
 * Every writer stores `new Date(...).toISOString()`, whose fixed-width UTC form
 * sorts lexicographically exactly as it sorts chronologically, so the raw text
 * is the key. Wrapping it in SQLite's `datetime()` truncates to whole seconds
 * and collapses sub-second writes into a tie group with no defined order;
 * `rowid` then settles exact-timestamp ties toward insertion order.
 *
 * Sub-second writes are normal here, not an edge case: a mid-turn steer lands
 * milliseconds after the message it interrupts, and `replaceChatMessages`
 * synthesizes a whole branch projection at `base + i` milliseconds.
 */
export const MESSAGE_ORDER_DESC = 'created_at DESC, rowid DESC';

/** Ascending form of {@link MESSAGE_ORDER_DESC}, for transcript order. */
export const MESSAGE_ORDER_ASC = 'created_at ASC, rowid ASC';

/**
 * Predicate selecting the message bound to `?2` in chat `?3` plus everything
 * ordered after it under {@link MESSAGE_ORDER_DESC}. An unknown id makes the
 * subquery NULL, so nothing matches and no rows are touched.
 *
 * Placeholders are positional: the caller binds (chatId, messageId, chatId).
 */
export const MESSAGE_CUT_FROM_ID = `(created_at, rowid) >= (
            SELECT created_at, rowid FROM chat_messages WHERE id = ? AND chat_id = ?
          )`;
