/**
 * Pure helper for the conversation-history assembly in `router.ts`.
 *
 * The send path inserts the current user message into the store *before*
 * building the model context, so the history window must drop that copy to
 * avoid the model seeing it twice (it is appended explicitly as the final
 * user message). The drop must be by id, not by tail position: the previous
 * assistant reply persists asynchronously at stream end (createdAt = write
 * time), so a fast follow-up message lands *before* it and the tail becomes
 * `[..., u_new, a_prev]` — a positional pop then misses the duplicate
 * (observed in the 2026-08-28 E2E request recording: every multi-turn user
 * message was injected twice).
 *
 * The regenerate path inserts nothing; its truncation guarantees the
 * triggering user message is the tail, so it keeps the positional pop.
 *
 * This module is pure so it can be unit-tested under plain Node/vitest —
 * `router.ts` itself imports `better-sqlite3` via `storage`, which is
 * compiled against Electron's ABI and can't load outside Electron.
 */

export interface HistoryMessageLike {
  id: string;
  role: string;
}

/**
 * @param historyAsc Chat history, chronologically ascending (oldest first).
 * @param currentUserMessageId Id of the just-inserted user message on the
 *   send path; `undefined` on the regenerate path.
 */
export function dropCurrentUserMessage<T extends HistoryMessageLike>(
  historyAsc: T[],
  currentUserMessageId: string | undefined,
): T[] {
  if (currentUserMessageId) {
    return historyAsc.filter((item) => item.id !== currentUserMessageId);
  }
  if (historyAsc.length > 0 && historyAsc[historyAsc.length - 1].role === 'user') {
    return historyAsc.slice(0, -1);
  }
  return historyAsc;
}
