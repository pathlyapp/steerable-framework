/**
 * `ChatPanel` — the foundational orchestration shell.
 *
 * Composes `MessageList` + a minimal input row + a stop button. The component
 * is intentionally minimal so consumers can layer on the production-grade
 * extras (model selector, tool toggles, mention picker, slash commands, file
 * upload, automation alerts, share-image button, multi-tab agent switcher,
 * orchestration sidebar) without having to fork the chat itself.
 *
 * Production callers in `deeppath/apps/web` will build their fancier
 * `<ChatHeader />` and `<ChatInput />` separately and feed `ChatPanel` only
 * the `messages` + `onSubmit` props — the rest is rendered around it.
 */

import { useCallback, useState } from 'react';
import type { ChatMessage, ToolCall, ToolResult } from '@steerable/agent-protocol';
import { MessageList, type MessageRendererProps } from './MessageList';
import { ToolCallRenderer } from './ToolCallRenderer';
import { cn } from './cn';

export interface ChatPanelProps {
  messages: ChatMessage[];
  isStreaming?: boolean;
  /**
   * Called when the user submits the input. Receives the trimmed string.
   * The parent is responsible for calling `useChatStream().sendUserMessage`.
   */
  onSubmit: (input: { content: string }) => void | Promise<void>;
  onCancel?: () => void;
  className?: string;
  inputPlaceholder?: string;
  /** Slot rendered above the message list (header, agent picker, …). */
  header?: React.ReactNode;
  /**
   * Tool-call renderer. Default uses the framework's `ToolCallRenderer`;
   * pass `() => null` to suppress, or your own component to inject a
   * domain-specific renderer (CIFLog cards, SQL query previews, …).
   */
  renderToolCall?: (call: ToolCall, result?: ToolResult) => React.ReactNode;
  /** Empty-state slot for the message list. */
  emptyState?: React.ReactNode;
  /** Disable the input (e.g. during initial chat load). */
  disabled?: boolean;
}

function defaultRenderToolCall(call: ToolCall, result?: ToolResult) {
  return <ToolCallRenderer key={call.id} call={call} result={result} />;
}

export function ChatPanel(props: ChatPanelProps) {
  const {
    messages,
    isStreaming = false,
    onSubmit,
    onCancel,
    className,
    inputPlaceholder = 'Send a message…',
    header,
    renderToolCall = defaultRenderToolCall,
    emptyState,
    disabled = false,
  } = props;
  const [draft, setDraft] = useState('');

  const handleSubmit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      const trimmed = draft.trim();
      if (!trimmed || disabled) return;
      setDraft('');
      await onSubmit({ content: trimmed });
    },
    [draft, disabled, onSubmit],
  );

  const renderMessage = useCallback(
    (rendererProps: MessageRendererProps) => {
      const { message, isLastAssistant, isStreaming: streaming } = rendererProps;
      const isAssistant = message.role === 'assistant';
      return (
        <div
          key={message.id ?? rendererProps.index}
          data-role={message.role}
          className={cn(
            'flex w-full',
            isAssistant ? 'justify-start' : 'justify-end',
          )}
        >
          <div
            className={cn(
              'flex max-w-[80%] flex-col gap-2',
              isAssistant ? 'items-start' : 'items-end',
            )}
          >
            <div
              className={cn(
                'rounded-agent-md border px-3 py-2 text-sm leading-relaxed',
                isAssistant
                  ? 'border-agent-border bg-agent-muted text-agent-foreground'
                  : 'border-transparent bg-agent-accent text-agent-accent-foreground',
              )}
            >
              <div className="whitespace-pre-wrap">
                {message.content || (isLastAssistant && streaming ? '…' : '')}
              </div>
              {isLastAssistant && streaming ? (
                <span
                  aria-hidden
                  className="ml-0.5 inline-block h-3 w-1.5 align-baseline bg-agent-foreground animate-agent-cursor-blink"
                />
              ) : null}
            </div>
            {Array.isArray(message.toolCalls) && message.toolCalls.length > 0 ? (
              <div className="flex w-full flex-col gap-1.5">
                {message.toolCalls.map((call) =>
                  renderToolCall(
                    call,
                    // The hook attaches a single `toolResult` to the message;
                    // production callers that want per-call results should
                    // override `renderToolCall` and look them up themselves.
                    message.toolResult,
                  ),
                )}
              </div>
            ) : null}
          </div>
        </div>
      );
    },
    [renderToolCall],
  );

  return (
    <div
      className={cn(
        'flex h-full w-full flex-col bg-agent-canvas text-agent-foreground',
        className,
      )}
    >
      {header}
      <div className="flex-1 overflow-hidden">
        <MessageList
          messages={messages}
          isStreaming={isStreaming}
          renderMessage={renderMessage}
          emptyState={emptyState}
        />
      </div>
      <form
        className="flex items-center gap-2 border-t border-agent-border bg-agent-canvas px-3 py-2"
        onSubmit={handleSubmit}
      >
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={inputPlaceholder}
          disabled={disabled}
          className={cn(
            'flex-1 rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 text-sm text-agent-foreground placeholder:text-agent-muted-foreground',
            'focus:border-agent-accent focus:outline-none focus:ring-1 focus:ring-agent-accent',
            disabled && 'opacity-50',
          )}
        />
        {isStreaming && onCancel ? (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-agent-md border border-agent-border px-3 py-2 text-sm text-agent-foreground hover:bg-agent-muted"
          >
            Stop
          </button>
        ) : (
          <button
            type="submit"
            disabled={disabled || !draft.trim()}
            className={cn(
              'rounded-agent-md bg-agent-accent px-3 py-2 text-sm font-medium text-agent-accent-foreground',
              (disabled || !draft.trim()) && 'opacity-50',
            )}
          >
            Send
          </button>
        )}
      </form>
    </div>
  );
}
