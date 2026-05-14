export interface ToolCall {
  id: string;
  name: string;
  arguments: {
    [k: string]: any;
  };
}
