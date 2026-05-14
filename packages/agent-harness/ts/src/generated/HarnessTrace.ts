/**
 * Run-scoped trace summary for one harness execution (one user turn). Pairs with TraceSpan and TraceEvent records.
 */
export interface HarnessTrace {
  /**
   * Unique trace identifier (e.g. 'tr_<hex>'). Stable across the lifetime of a single run.
   */
  traceId: string;
  userId?: string | null;
  chatId?: string | null;
  sessionId?: string | null;
  /**
   * The chat message id this trace was attached to once persisted.
   */
  assistantMessageId?: string | null;
  status: "running" | "completed" | "error" | "cancelled" | "budget_exhausted";
  durationMs?: number | null;
  hadError: boolean;
  errorMessage?: string | null;
  eventCount: number;
  spanCount: number;
  totalTokens?: number | null;
  /**
   * Primary LLM model identifier used during this trace.
   */
  modelId?: string | null;
  /**
   * Epoch milliseconds when the run started.
   */
  startedAtMs?: number | null;
  createdAt: string;
  updatedAt: string;
}
