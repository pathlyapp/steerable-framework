import type { ContentPart } from "./ContentPart.js";
import type { ToolCall } from "./ToolCall.js";
import type { ToolResult } from "./ToolResult.js";
export interface ChatMessage {
  id: string;
  chatId?: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  /**
   * Structured content (Wave 1). Optional and additive: when present it is authoritative and `content` is its plain-text projection; when absent the message is text-only and `content` is the payload. Producers that emit multimodal messages MUST also fill `content` so text-only consumers keep working.
   */
  parts?: ContentPart[];
  agentId?: string;
  toolCalls?: ToolCall[];
  toolResult?: ToolResult;
  createdAt: string;
  updatedAt?: string;
  [k: string]: any;
}
