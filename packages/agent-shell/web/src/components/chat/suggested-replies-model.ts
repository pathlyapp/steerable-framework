/**
 * 从助手消息 metadata 读出持久化的追问建议（刷新后仍能画在最后一条回复下）。
 */
import type { ChatMessage } from '@steerable/agent-protocol';

export interface SuggestedRepliesState {
  messageId: string;
  suggestions: string[];
}

function readSuggestedReplies(metadata: unknown): string[] | null {
  if (typeof metadata !== 'string' || !metadata) return null;
  try {
    const parsed = JSON.parse(metadata) as { suggestedReplies?: unknown };
    if (
      Array.isArray(parsed.suggestedReplies) &&
      parsed.suggestedReplies.length > 0 &&
      parsed.suggestedReplies.every((item) => typeof item === 'string' && item.trim())
    ) {
      return parsed.suggestedReplies.map((item) => item.trim()).slice(0, 8);
    }
  } catch {
    return null;
  }
  return null;
}

/** 从新→旧找最近一条带 suggestedReplies 的助手消息。 */
export function extractLatestSuggestedReplies(
  messages: Array<ChatMessage & { messageMetadata?: string | null }> | undefined,
): SuggestedRepliesState | null {
  if (!messages || messages.length === 0) return null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role !== 'assistant') continue;
    const suggestions = readSuggestedReplies(message.messageMetadata);
    if (suggestions) return { messageId: message.id, suggestions };
  }
  return null;
}
