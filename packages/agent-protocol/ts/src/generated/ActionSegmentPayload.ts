/**
 * Payload of an action-segment card -- a strip of tool / action invocations the agent ran inline within an assistant message. Each segment carries a kind (the action type), a status, and arbitrary args/output payload that downstream renderers pretty-print.
 */
export interface ActionSegmentPayload {
  segments: {
    id: string;
    /**
     * Action / tool name, e.g. 'create_task' or 'search.web'.
     */
    kind: string;
    status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
    label?: string | null;
    args?: any;
    output?: any;
    error?: string | null;
    startedAt?: string | null;
    finishedAt?: string | null;
    [k: string]: any;
  }[];
  [k: string]: any;
}
