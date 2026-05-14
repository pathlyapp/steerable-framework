export type HarnessTraceStatus =
  | "running"
  | "completed"
  | "error"
  | "cancelled"
  | "budget_exhausted";

export interface HarnessTrace {
  traceId: string;
  userId?: string | null;
  chatId?: string | null;
  sessionId?: string | null;
  assistantMessageId?: string | null;
  status: HarnessTraceStatus;
  durationMs?: number | null;
  hadError?: boolean;
  errorMessage?: string | null;
  eventCount?: number;
  spanCount?: number;
  totalTokens?: number | null;
  modelId?: string | null;
  startedAtMs?: number | null;
  createdAt: string;
  updatedAt: string;
  [k: string]: unknown;
}
