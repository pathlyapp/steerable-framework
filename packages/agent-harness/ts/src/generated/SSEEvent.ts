export interface SSEEvent {
  type:
    | "content"
    | "error"
    | "agent"
    | "orchestration"
    | "loader-hint"
    | "keepalive"
    | "done"
    | "budget_exhausted"
    | "tool_call"
    | "tool_result";
  event?: string;
  content?: string;
  hint?: string;
  message?: string;
  code?: string;
  orchestrationGroupId?: string;
  taskId?: string;
  messageId?: string;
  payload?: {
    [k: string]: any;
  };
  [k: string]: any;
}
