export interface ToolResult {
  success: boolean;
  terminal?: boolean;
  needsFollowup?: boolean;
  nextAction?: string;
  message?: string;
  error?: string;
  data?: {
    [k: string]: any;
  };
  [k: string]: any;
}
