export type SSEEvent = {
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
} & (
  | {
      type: "content";
      content?: string;
      payload?: {
        text: string;
        roundIndex: number;
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "error";
      message?: string;
      code?: string;
      payload?: {
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "agent";
      event:
        | "session.start"
        | "session.end"
        | "round.start"
        | "round.end"
        | "assistant.done"
        | "budget_exhausted"
        | "error";
      payload?: {
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "orchestration";
      event?: string;
      orchestrationGroupId?: string;
      taskId?: string;
      content?: string;
      payload?: {
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "loader-hint";
      hint: string;
      [k: string]: any;
    }
  | {
      type: "keepalive";
      [k: string]: any;
    }
  | {
      type: "done";
      [k: string]: any;
    }
  | {
      type: "budget_exhausted";
      payload: {
        limitKind: string;
        budgetState: {
          [k: string]: any;
        };
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "tool_call";
      payload: {
        [k: string]: any;
      };
      [k: string]: any;
    }
  | {
      type: "tool_result";
      payload: {
        [k: string]: any;
      };
      [k: string]: any;
    }
);
