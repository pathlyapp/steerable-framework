export interface ToolResult {
  success: boolean;
  terminal?: boolean;
  needsFollowup?: boolean;
  nextAction?: string;
  message?: string;
  error?: string;
  data?: Record<string, unknown>;
  [k: string]: unknown;
}
