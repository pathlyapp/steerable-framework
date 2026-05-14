/**
 * `useChatStream` — own the in-flight chat lifecycle.
 *
 * Caller plugs in a transport (fetch+SSE, IPC bridge, sidecar, …) that yields
 * `SSEEvent` instances; the hook turns those into the local `ChatMessage[]`
 * that `<MessageList>` and `<ChatPanel>` render. The hook is intentionally
 * unaware of HTTP, of which provider serves the events, and of any specific
 * sequencing rules — those live in the framework spec and the caller's
 * transport.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import type {
  ChatMessage,
  SSEEvent,
  ToolCall,
  ToolResult,
} from '@steerable/agent-protocol';

export interface ChatStreamSendInput {
  /** The user's free-text message. */
  content: string;
  /** Optional structured metadata (mention list, tools requested, etc.). */
  metadata?: Record<string, unknown>;
}

export interface ChatStreamTransport {
  /**
   * Called when the user submits a message. The transport is expected to:
   *   1. POST/IPC the message to the backend,
   *   2. consume the SSE stream and call `onEvent` for each parsed event,
   *   3. resolve when the stream terminates (or reject on error).
   *
   * Returning a function from the promise is a `cancel` handle the hook will
   * call on unmount or when the user clicks "stop".
   */
  stream: (
    input: ChatStreamSendInput,
    onEvent: (event: SSEEvent) => void,
  ) => Promise<void | (() => void)>;
}

export interface UseChatStreamOptions {
  /** Required transport handle; see `ChatStreamTransport`. */
  transport: ChatStreamTransport;
  /** Initial message history (persisted from previous turns). */
  initialMessages?: ChatMessage[];
  /**
   * Map a tool-result event to a standalone `tool` ChatMessage. Default
   * returns null, which keeps tool results inline on the assistant message
   * that owns the corresponding call.
   */
  toolResultToMessage?: (result: ToolResult) => ChatMessage | null;
  /**
   * Called for every event that doesn't have a built-in handler — useful for
   * `loader-hint`, `agent`, `orchestration`, automation, search-source, etc.
   * Receivers can stash these into their own state.
   */
  onUnknownEvent?: (event: SSEEvent) => void;
}

export interface UseChatStreamReturn {
  messages: ChatMessage[];
  isStreaming: boolean;
  sendUserMessage: (input: ChatStreamSendInput) => Promise<void>;
  cancel: () => void;
  /** Replace the message buffer (e.g. when switching chat). */
  setMessages: (messages: ChatMessage[]) => void;
  /** Append a single message without going through the transport. */
  appendMessage: (message: ChatMessage) => void;
}

type Action =
  | { type: 'reset'; messages: ChatMessage[] }
  | { type: 'append'; message: ChatMessage }
  | { type: 'patch-last-assistant'; patch: Partial<ChatMessage> }
  | { type: 'append-content'; delta: string }
  | { type: 'append-tool-call'; call: ToolCall }
  | { type: 'attach-tool-result'; result: ToolResult }
  | { type: 'finalize-assistant' };

interface State {
  messages: ChatMessage[];
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'reset':
      return { messages: action.messages };
    case 'append':
      return { messages: [...state.messages, action.message] };
    case 'patch-last-assistant': {
      const idx = findLastAssistantIndex(state.messages);
      if (idx === -1) return state;
      const patched: ChatMessage = {
        ...state.messages[idx],
        ...action.patch,
      } as ChatMessage;
      const next = state.messages.slice();
      next[idx] = patched;
      return { messages: next };
    }
    case 'append-content': {
      const idx = findLastAssistantIndex(state.messages);
      if (idx === -1) return state;
      const target = state.messages[idx];
      const patched: ChatMessage = {
        ...target,
        content: (target.content ?? '') + action.delta,
      } as ChatMessage;
      const next = state.messages.slice();
      next[idx] = patched;
      return { messages: next };
    }
    case 'append-tool-call': {
      const idx = findLastAssistantIndex(state.messages);
      if (idx === -1) return state;
      const target = state.messages[idx];
      const calls = Array.isArray(target.toolCalls) ? [...target.toolCalls] : [];
      calls.push(action.call);
      const patched: ChatMessage = { ...target, toolCalls: calls } as ChatMessage;
      const next = state.messages.slice();
      next[idx] = patched;
      return { messages: next };
    }
    case 'attach-tool-result': {
      // The protocol allows `ChatMessage.toolResult`, but most renderers want
      // to see results inline on the assistant message that owns the call.
      const idx = findLastAssistantIndex(state.messages);
      if (idx === -1) return state;
      const target = state.messages[idx];
      const patched: ChatMessage = {
        ...target,
        toolResult: action.result,
      } as ChatMessage;
      const next = state.messages.slice();
      next[idx] = patched;
      return { messages: next };
    }
    case 'finalize-assistant':
      return state;
    default:
      return state;
  }
}

function findLastAssistantIndex(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') return i;
  }
  return -1;
}

function newAssistantPlaceholder(): ChatMessage {
  return {
    id: `placeholder_${Date.now()}_${Math.floor(Math.random() * 1e6)}`,
    role: 'assistant',
    content: '',
    createdAt: new Date().toISOString(),
  };
}

function newUserMessage(content: string): ChatMessage {
  return {
    id: `user_${Date.now()}_${Math.floor(Math.random() * 1e6)}`,
    role: 'user',
    content,
    createdAt: new Date().toISOString(),
  };
}

function pickContentDelta(event: SSEEvent): string | null {
  if (typeof event.content === 'string' && event.content.length > 0) {
    return event.content;
  }
  // Some transports stash the delta in `payload.delta` or `payload.content`.
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  if (typeof payload.delta === 'string') return payload.delta;
  if (typeof payload.content === 'string') return payload.content;
  return null;
}

function pickToolCall(event: SSEEvent): ToolCall | null {
  const payload = event.payload as unknown;
  if (
    payload &&
    typeof payload === 'object' &&
    'id' in payload &&
    'name' in payload &&
    'arguments' in payload
  ) {
    return payload as ToolCall;
  }
  return null;
}

function pickToolResult(event: SSEEvent): ToolResult | null {
  const payload = event.payload as unknown;
  if (payload && typeof payload === 'object' && 'success' in payload) {
    return payload as ToolResult;
  }
  return null;
}

export function useChatStream(
  options: UseChatStreamOptions,
): UseChatStreamReturn {
  const [state, dispatch] = useReducer(reducer, {
    messages: options.initialMessages ?? [],
  });
  const isStreamingRef = useRef(false);
  const cancelRef = useRef<(() => void) | null>(null);
  const [, forceRerender] = useReducer((x: number) => x + 1, 0);

  const setStreaming = useCallback((next: boolean) => {
    isStreamingRef.current = next;
    forceRerender();
  }, []);

  const handleEvent = useCallback(
    (event: SSEEvent) => {
      switch (event.type) {
        case 'content': {
          const delta = pickContentDelta(event);
          if (delta) dispatch({ type: 'append-content', delta });
          return;
        }
        case 'tool_call': {
          const call = pickToolCall(event);
          if (call) dispatch({ type: 'append-tool-call', call });
          return;
        }
        case 'tool_result': {
          const result = pickToolResult(event);
          if (!result) return;
          const newMsg = options.toolResultToMessage?.(result);
          if (newMsg) {
            dispatch({ type: 'append', message: newMsg });
          } else {
            dispatch({ type: 'attach-tool-result', result });
          }
          return;
        }
        case 'done':
          dispatch({ type: 'finalize-assistant' });
          return;
        case 'error': {
          const msg = event.message ?? 'stream error';
          dispatch({
            type: 'patch-last-assistant',
            patch: { content: `[stream error] ${msg}` },
          });
          return;
        }
        case 'budget_exhausted': {
          dispatch({
            type: 'patch-last-assistant',
            patch: {
              content:
                event.message ??
                '[budget exhausted] the harness stopped this run.',
            },
          });
          return;
        }
        case 'agent':
        case 'orchestration':
        case 'loader-hint':
        case 'keepalive':
        default:
          options.onUnknownEvent?.(event);
      }
    },
    [options],
  );

  const sendUserMessage = useCallback(
    async (input: ChatStreamSendInput) => {
      if (isStreamingRef.current) {
        return;
      }
      dispatch({ type: 'append', message: newUserMessage(input.content) });
      dispatch({ type: 'append', message: newAssistantPlaceholder() });
      setStreaming(true);
      try {
        const cancel = await options.transport.stream(input, handleEvent);
        cancelRef.current = typeof cancel === 'function' ? cancel : null;
      } catch (err) {
        dispatch({
          type: 'patch-last-assistant',
          patch: {
            content:
              '[stream error] ' +
              (err instanceof Error ? err.message : String(err)),
          },
        });
      } finally {
        cancelRef.current = null;
        setStreaming(false);
      }
    },
    [handleEvent, options.transport, setStreaming],
  );

  const cancel = useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    setStreaming(false);
  }, [setStreaming]);

  const setMessages = useCallback((messages: ChatMessage[]) => {
    dispatch({ type: 'reset', messages });
  }, []);

  const appendMessage = useCallback((message: ChatMessage) => {
    dispatch({ type: 'append', message });
  }, []);

  useEffect(() => {
    return () => {
      if (cancelRef.current) {
        cancelRef.current();
        cancelRef.current = null;
      }
    };
  }, []);

  return useMemo<UseChatStreamReturn>(
    () => ({
      messages: state.messages,
      isStreaming: isStreamingRef.current,
      sendUserMessage,
      cancel,
      setMessages,
      appendMessage,
    }),
    [state.messages, sendUserMessage, cancel, setMessages, appendMessage],
  );
}
