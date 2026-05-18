/**
 * Payload of a suggested-replies card. A small set of single-shot quick-reply texts shown below the composer; clicking one sends that text as the next user message.
 */
export interface SuggestedRepliesPayload {
  /**
   * Quick-reply texts. Clients typically render the first ~6; backend should keep the list short.
   */
  suggestions: string[];
  [k: string]: any;
}
