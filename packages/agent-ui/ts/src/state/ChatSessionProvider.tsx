/**
 * `ChatSessionProvider` + `useChatSessionContext`
 *
 * Lifts the chat-session primitives out of the host app's god-context (e.g.
 * deeppath's `ChatUIContext`, deeppath-agent's hooks) into a framework-owned
 * provider. Concretely it owns:
 *
 *   - the streaming transcript: `messages`, `isStreaming`, `streamingMessageId`
 *   - send / cancel / clear actions
 *   - the composer (draft, send, cancel, key handlers)
 *
 * App-specific concerns that don't belong here (file uploads, geolocation,
 * suggestion banners, chat tool toggles, chat list / agent selector) stay in
 * the host app's own context. The `extras` slot lets apps thread custom
 * values through the same provider so chat cards can call host actions
 * without prop-drilling.
 *
 * Usage in a host app:
 *
 *   <ChatSessionProvider
 *     value={{ ...useChatSession({ transport }), extras: { ...appHooks } }}
 *   >
 *     <ChatPanel.Root>...</ChatPanel.Root>
 *   </ChatSessionProvider>
 *
 * Cards / shell pieces consume via `useChatSessionContext()` so they don't
 * import the host's context directly. This is the seam that lets the same
 * compound `ChatPanel` work in deeppath, deeppath-agent, and the framework
 * `examples/web-shell`.
 */
import * as React from 'react';
import type { ChatMessage } from '@steerable/agent-protocol';
import type { UseChatSessionReturn } from './useChatSession.js';

export interface ChatSessionContextValue<TExtras = unknown> extends UseChatSessionReturn {
  /** Host-specific values threaded through the same provider (geo, files, ...). */
  extras?: TExtras;
}

const ChatSessionContext = React.createContext<ChatSessionContextValue | null>(null);

export interface ChatSessionProviderProps<TExtras = unknown> {
  value: ChatSessionContextValue<TExtras>;
  children: React.ReactNode;
}

export function ChatSessionProvider<TExtras = unknown>({
  value,
  children,
}: ChatSessionProviderProps<TExtras>) {
  return (
    <ChatSessionContext.Provider value={value as ChatSessionContextValue}>
      {children}
    </ChatSessionContext.Provider>
  );
}

/**
 * Read the active chat session. Throws if no `ChatSessionProvider` is
 * mounted above -- this is intentional: cards / shell pieces should be
 * usable both inside the provider (typical) and via direct props for
 * apps that haven't migrated yet (use `useOptionalChatSession()` for that).
 */
export function useChatSessionContext<TExtras = unknown>(): ChatSessionContextValue<TExtras> {
  const ctx = React.useContext(ChatSessionContext);
  if (!ctx) {
    throw new Error('useChatSessionContext must be used inside a <ChatSessionProvider />');
  }
  return ctx as ChatSessionContextValue<TExtras>;
}

/** Non-throwing variant for components that want to fall back to props. */
export function useOptionalChatSession<TExtras = unknown>():
  | ChatSessionContextValue<TExtras>
  | null {
  const ctx = React.useContext(ChatSessionContext);
  return ctx as ChatSessionContextValue<TExtras> | null;
}

/**
 * Convenience selector for the most-used slice in cards (`{ messages,
 * isStreaming, sendUserMessage, cancel }`). Returns null when there's no
 * provider so cards can opt into context-driven mode incrementally.
 */
export function useChatSessionSlice():
  | Pick<ChatSessionContextValue, 'messages' | 'isStreaming' | 'sendUserMessage' | 'cancel'>
  | null {
  const ctx = useOptionalChatSession();
  if (!ctx) return null;
  return {
    messages: ctx.messages,
    isStreaming: ctx.isStreaming,
    sendUserMessage: ctx.sendUserMessage,
    cancel: ctx.cancel,
  };
}

export type { ChatMessage };
