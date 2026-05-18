/**
 * Payload of a summary-message card. Used when the chat history was condensed: the card shows a markdown summary along with a count of how many original messages it replaces. Collapsed by default.
 */
export interface SummaryMessagePayload {
  /**
   * Markdown summary text.
   */
  body: string;
  summarizedCount?: number | null;
  status?: "pending" | "complete" | "failed" | null;
  /**
   * Free-form summary kind label (e.g. 'history_compaction').
   */
  type?: string | null;
  [k: string]: any;
}
