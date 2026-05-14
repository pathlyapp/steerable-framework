import type { ToolCall } from "./ToolCall";
import type { ToolResult } from "./ToolResult";

export interface ChatMessage {
  id: string;
  chatId?: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  agentId?: string;
  toolCalls?: ToolCall[];
  toolResult?: ToolResult;
  createdAt: string;
  updatedAt?: string;
  [k: string]: unknown;
}
