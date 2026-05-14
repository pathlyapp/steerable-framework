/**
 * A point-in-time event inside a HarnessTrace. Use this for things that have no measurable duration (notifications, decisions, lifecycle markers).
 */
export interface TraceEvent {
  /**
   * Optional storage primary key (cuid).
   */
  id?: string;
  traceId: string;
  /**
   * Coarse classification used for filtering/UI grouping.
   */
  kind:
    | "lifecycle"
    | "policy"
    | "budget"
    | "retry"
    | "tool_call"
    | "tool_result"
    | "llm_request"
    | "llm_response"
    | "completion"
    | "error"
    | "log"
    | "custom";
  /**
   * Event name (e.g. 'tool.invoked', 'budget.tokens.exhausted').
   */
  name: string;
  /**
   * Monotonic per-trace ordering value. Stored uniquely with traceId.
   */
  sequence: number;
  /**
   * Epoch milliseconds when the event was recorded.
   */
  timestampMs: number;
  /**
   * Optional duration when an event spans a brief work unit.
   */
  durationMs?: number | null;
  status?: "ok" | "warning" | "error" | null;
  /**
   * JSON-serializable payload, secret-redacted.
   */
  payload?: {
    [k: string]: any;
  } | null;
  createdAt?: string;
}
