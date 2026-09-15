/**
 * W7-1: pure helper for the interrupted-turn contract in `router.ts`.
 *
 * A turn whose process died mid-stream (crash / kill / lost stream) never
 * reaches the code that persists `completionStatus`, so the crash signature
 * is the surviving `turn_active:{chatId}` marker in `settings_kv` — written
 * before the stream starts, cleared only after the terminal assistant
 * message persists. User-cancelled and failed turns are settled states the
 * live process DID record: their marker is cleared and their assistant
 * message carries an explicit `completionStatus`, so neither is reported
 * here. Pure so it is unit-testable — `storage/index.ts` can't load under
 * plain Node/vitest (`better-sqlite3` is compiled against Electron's ABI).
 */

export interface InterruptedTurnInput {
  /** The `turn_active` marker survived — a turn began and never settled. */
  turnActive: boolean;
  /** A stream for this chat is live in THIS process right now. */
  streamActive: boolean;
  /** Role of the chat's newest persisted message, or null on an empty chat. */
  lastMessageRole: string | null;
}

/**
 * Interrupted ⟺ marker present, no live stream, and the store does not
 * already end on an assistant reply. The role check dismisses the stale
 * marker left by a crash between the assistant write and the marker clear
 * (adjacent statements, but the clear is deliberately last): the reply's
 * presence proves the turn settled.
 */
export function detectInterruptedTurn(input: InterruptedTurnInput): boolean {
  if (!input.turnActive || input.streamActive) return false;
  return input.lastMessageRole !== 'assistant';
}
