export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}
