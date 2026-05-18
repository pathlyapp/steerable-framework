/**
 * Payload of a tool-execution card. Unified representation of a single tool / MCP / local action invocation: name, status, args (input), output, and an optional human-readable summary. Both deeppath's ActionSegment and deeppath-agent's ExecutedActionsCard render off this same shape.
 */
export interface ToolExecutionPayload {
  /**
   * Stable id of this invocation.
   */
  id: string;
  /**
   * Tool name as exposed to the agent.
   */
  name: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  /**
   * One-line human summary for the row header.
   */
  summary?: string | null;
  args?: any;
  output?: any;
  error?: string | null;
  durationMs?: number | null;
  /**
   * Optional icon hint (lucide name or url).
   */
  icon?: string | null;
  expandable?: boolean;
  [k: string]: any;
}
