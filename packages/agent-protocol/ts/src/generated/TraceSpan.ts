export type SpanKind =
  | "llm"
  | "tool"
  | "policy"
  | "budget"
  | "retry"
  | "completion"
  | "transport"
  | "storage"
  | "custom";

export type SpanStatus = "ok" | "error" | "timeout" | "cancelled" | "running";

export interface TraceSpan {
  spanId: string;
  traceId?: string | null;
  parentSpanId?: string | null;
  name: string;
  kind?: SpanKind;
  startMs: number;
  endMs?: number | null;
  durationMs?: number | null;
  status: SpanStatus;
  attrs?: Record<string, unknown>;
  [k: string]: unknown;
}
