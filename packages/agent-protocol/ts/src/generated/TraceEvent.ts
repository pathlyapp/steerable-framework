export type TraceEventKind =
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

export interface TraceEvent {
  id?: string;
  traceId: string;
  kind: TraceEventKind;
  name: string;
  sequence: number;
  timestampMs: number;
  durationMs?: number | null;
  status?: "ok" | "warning" | "error" | null;
  payload?: Record<string, unknown> | null;
  createdAt?: string;
  [k: string]: unknown;
}
