/**
 * `MessageList` — render a list of `ChatMessage`s with auto-scroll-to-bottom
 * behaviour. The component is intentionally **headless**: it knows the list
 * shape but lets the caller render every individual bubble.
 *
 * Why a render prop instead of a fixed bubble component? Because every
 * consumer wants to layer additional UI (markdown, search-source pills,
 * mention chips, action renderers) onto the assistant message and we don't
 * want to ship every pluggable variant from `deeppath/apps/web` here.
 */

import { useEffect, useMemo, useRef } from 'react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { cn } from './cn';

export interface MessageRendererProps {
  message: ChatMessage;
  index: number;
  isLastAssistant: boolean;
  isStreaming: boolean;
}

export interface MessageListProps {
  messages: ChatMessage[];
  isStreaming?: boolean;
  className?: string;
  /**
   * Bubble renderer. If omitted, `MessageList` falls back to the built-in
   * `DefaultMessageBubble` which just renders role + content.
   */
  renderMessage?: (props: MessageRendererProps) => React.ReactNode;
  /**
   * If true (default), scrolls the container to the bottom every time the
   * message list grows or the last assistant message gets new content.
   */
  autoScroll?: boolean;
  /**
   * Slot rendered above the first message — typically a "start of conversation"
   * marker or a system prompt summary.
   */
  header?: React.ReactNode;
  /**
   * Slot rendered when `messages` is empty. Production callers usually drop
   * an `<EmptyChat />` here.
   */
  emptyState?: React.ReactNode;
}

function DefaultMessageBubble({ message, isLastAssistant, isStreaming }: MessageRendererProps) {
  const isAssistant = message.role === 'assistant';
  return (
    <div
      data-role={message.role}
      className={cn(
        'flex w-full',
        isAssistant ? 'justify-start' : 'justify-end',
      )}
    >
      <div
        className={cn(
          'max-w-[80%] rounded-agent-md border px-3 py-2 text-sm leading-relaxed',
          isAssistant
            ? 'border-agent-border bg-agent-muted text-agent-foreground'
            : 'border-transparent bg-agent-accent text-agent-accent-foreground',
        )}
      >
        <div className="whitespace-pre-wrap">{message.content || (isLastAssistant && isStreaming ? '…' : '')}</div>
        {isLastAssistant && isStreaming ? (
          <span
            aria-hidden
            className="ml-0.5 inline-block h-3 w-1.5 align-baseline bg-agent-foreground animate-agent-cursor-blink"
          />
        ) : null}
      </div>
    </div>
  );
}

export function MessageList(props: MessageListProps) {
  const {
    messages,
    isStreaming = false,
    className,
    renderMessage,
    autoScroll = true,
    header,
    emptyState,
  } = props;
  const containerRef = useRef<HTMLDivElement>(null);

  const lastAssistantIndex = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return i;
    }
    return -1;
  }, [messages]);

  // Scroll to bottom on growth or when the streaming assistant gets longer.
  const lastAssistantContent =
    lastAssistantIndex >= 0 ? messages[lastAssistantIndex].content : '';

  useEffect(() => {
    if (!autoScroll) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [autoScroll, messages.length, lastAssistantContent]);

  const Renderer = renderMessage ?? DefaultMessageBubble;

  return (
    <div
      ref={containerRef}
      className={cn(
        'flex h-full w-full flex-col gap-3 overflow-y-auto bg-agent-canvas px-4 py-3',
        className,
      )}
      role="log"
      aria-live="polite"
    >
      {header}
      {messages.length === 0 ? (
        emptyState ?? (
          <div className="m-auto text-sm text-agent-muted-foreground">
            No messages yet.
          </div>
        )
      ) : (
        messages.map((message, index) => (
          <Renderer
            key={message.id ?? index}
            message={message}
            index={index}
            isLastAssistant={index === lastAssistantIndex}
            isStreaming={isStreaming}
          />
        ))
      )}
    </div>
  );
}
