/**
 * A timed span within a HarnessTrace. Each span captures a logical step (LLM call, tool call, planning step, etc.).
 */
export interface TraceSpan {
  /**
   * Span identifier, unique within its parent trace (e.g. 'step_3').
   */
  spanId: string;
  /**
   * Parent HarnessTrace id. Optional for in-flight spans before flush.
   */
  traceId?: string | null;
  /**
   * Optional parent span for nested execution graphs.
   */
  parentSpanId?: string | null;
  /**
   * Logical operation name (e.g. 'llm.generate', 'tool.shell.exec', 'policy.decide').
   */
  name: string;
  kind?: "llm" | "tool" | "policy" | "budget" | "retry" | "completion" | "transport" | "storage" | "custom";
  /**
   * Epoch milliseconds when the span opened.
   */
  startMs: number;
  /**
   * Epoch milliseconds when the span closed. Null while still in flight.
   */
  endMs?: number | null;
  durationMs?: number | null;
  status: "ok" | "error" | "timeout" | "cancelled" | "running";
  /**
   * Free-form, JSON-serializable, secret-redacted attributes.
   */
  attrs?: {
    [k: string]: any;
  };
}
