/**
 * `ChatPanel` — the orchestration shell, now exposed as a Radix-style
 * compound: `ChatPanel.Root / .Header / .Messages / .Input / .Empty /
 * .StreamingStatus`.
 *
 * Compound API
 * ------------
 *   <ChatPanel.Root className="…">
 *     <ChatPanel.Header>{customHeader}</ChatPanel.Header>
 *     <ChatPanel.Messages
 *       messages={messages}
 *       isStreaming={isStreaming}
 *       renderMessage={renderRichBubble}
 *       emptyState={<ChatPanel.Empty onSelectPrompt={…} />}
 *     />
 *     <ChatPanel.Input
 *       value={draft}
 *       onChange={setDraft}
 *       onSubmit={send}
 *       onCancel={cancel}
 *       isStreaming={isStreaming}
 *     />
 *   </ChatPanel.Root>
 *
 * Each sub-component owns one slot of the chat. The .Messages and .Input
 * subs accept `as` / `slots` escape hatches so consumers can replace any one
 * leaf without re-implementing the rest.
 *
 * Monolithic alias
 * ----------------
 *   <ChatPanel messages={messages} onSubmit={onSubmit} … />
 *
 * The 0.2.x props remain valid — they internally render the compound. New
 * consumers should reach for the compound; the alias exists to make the
 * 0.2 -> 0.3 cutover non-breaking for in-flight integrations.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import type { ChatMessage, ToolCall, ToolResult } from '@steerable/agent-protocol';
import { MessageList, type MessageRendererProps } from './MessageList.js';
import { ToolCallRenderer } from './ToolCallRenderer.js';
import { cn } from './cn.js';
import { useChatSessionContext } from '../state/ChatSessionProvider.js';

// ---------------------------------------------------------------------------
// Root
// ---------------------------------------------------------------------------

export interface ChatPanelRootProps {
  children: React.ReactNode;
  className?: string;
  /**
   * Opt out of the default `bg-agent-canvas text-agent-foreground` styling
   * — handy when the consumer's host container already paints the
   * background (e.g. deeppath's `bg-card` rounded shell) and you don't want
   * two layers fighting over which wins in the cascade.
   */
  unstyled?: boolean;
  /** Render as a different host element (e.g. 'section', 'main'). */
  as?: keyof React.JSX.IntrinsicElements;
}

function ChatPanelRoot({
  children,
  className,
  unstyled = false,
  as: As = 'div',
}: ChatPanelRootProps) {
  // The `as any` keeps the JSX surface flexible without forcing every host
  // element's prop variance through the inference machinery.
  const Component = As as React.ElementType;
  return (
    <Component
      className={cn(
        'flex h-full w-full flex-col',
        unstyled ? null : 'bg-agent-canvas text-agent-foreground',
        className,
      )}
    >
      {children}
    </Component>
  );
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

export interface ChatPanelHeaderProps {
  children?: React.ReactNode;
  className?: string;
}

function ChatPanelHeader({ children, className }: ChatPanelHeaderProps) {
  if (!children) return null;
  return (
    <div
      className={cn(
        'flex items-center gap-2 border-b border-agent-border bg-agent-canvas px-3 py-2',
        className,
      )}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

export interface ChatPanelMessagesProps {
  messages: ChatMessage[];
  isStreaming?: boolean;
  renderMessage?: (props: MessageRendererProps) => React.ReactNode;
  renderToolCall?: (call: ToolCall, result?: ToolResult) => React.ReactNode;
  emptyState?: React.ReactNode;
  className?: string;
}

function defaultRenderToolCall(call: ToolCall, result?: ToolResult) {
  return <ToolCallRenderer key={call.id} call={call} result={result} />;
}

function ChatPanelMessages(props: ChatPanelMessagesProps) {
  const {
    messages,
    isStreaming = false,
    renderMessage,
    renderToolCall = defaultRenderToolCall,
    emptyState,
    className,
  } = props;

  const renderBubble = useCallback(
    (rendererProps: MessageRendererProps) => {
      if (renderMessage) return renderMessage(rendererProps);
      return (
        <DefaultRichBubble
          {...rendererProps}
          renderToolCall={renderToolCall}
        />
      );
    },
    [renderMessage, renderToolCall],
  );

  return (
    <div className={cn('flex-1 overflow-hidden', className)}>
      <MessageList
        messages={messages}
        isStreaming={isStreaming}
        renderMessage={renderBubble}
        emptyState={emptyState}
      />
    </div>
  );
}

function DefaultRichBubble({
  message,
  isLastAssistant,
  isStreaming,
  index,
  renderToolCall,
}: MessageRendererProps & {
  renderToolCall: (call: ToolCall, result?: ToolResult) => React.ReactNode;
}) {
  const isAssistant = message.role === 'assistant';
  return (
    <div
      key={message.id ?? index}
      data-role={message.role}
      className={cn('flex w-full', isAssistant ? 'justify-start' : 'justify-end')}
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
            {message.content || (isLastAssistant && isStreaming ? '…' : '')}
          </div>
          {isLastAssistant && isStreaming ? (
            <span
              aria-hidden
              className="ml-0.5 inline-block h-3 w-1.5 align-baseline bg-agent-foreground animate-agent-cursor-blink"
            />
          ) : null}
        </div>
        {Array.isArray(message.toolCalls) && message.toolCalls.length > 0 ? (
          <div className="flex w-full flex-col gap-1.5">
            {message.toolCalls.map((call) => renderToolCall(call, message.toolResult))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Input
//
// Multi-line textarea with auto-grow, IME-safe keyboard handling, and a
// send/stop affordance. This is the rich default lifted from
// deeppath-agent/apps/web/src/components/chat/ChatInput.tsx — minus the
// agent-pill and settings gear, which live in agent-specific slots the apps
// can drop into the toolbar via `toolbarLeft`/`toolbarRight` props.
// ---------------------------------------------------------------------------

const MIN_TEXTAREA_PX = 44;
const MAX_TEXTAREA_PX = 220;

const IS_MAC =
  typeof navigator !== 'undefined' &&
  /Mac|iPod|iPhone|iPad/.test(navigator.platform || '');
const MOD_LABEL = IS_MAC ? '⌘' : 'Ctrl';

export interface ChatPanelInputHandle {
  focus: () => void;
  focusAtEnd: () => void;
}

export interface ChatPanelInputProps {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void | Promise<void>;
  onCancel?: () => void;
  isStreaming?: boolean;
  disabled?: boolean;
  placeholder?: string;
  /**
   * Keyboard mode:
   *   - 'cmd-enter' (default for the rich Input): Enter = newline,
   *     Cmd/Ctrl+Enter = send. IME-safe.
   *   - 'enter': Enter = send, Shift+Enter = newline. Matches the cloud
   *     chat product.
   */
  keyMode?: 'cmd-enter' | 'enter';
  /** Optional content slot rendered to the left of the send button. */
  toolbarLeft?: React.ReactNode;
  /** Optional content slot rendered to the right of the textarea. */
  toolbarRight?: React.ReactNode;
  className?: string;
}

export const ChatPanelInput = forwardRef<ChatPanelInputHandle, ChatPanelInputProps>(
  function ChatPanelInput(
    {
      value,
      onChange,
      onSubmit,
      onCancel,
      isStreaming = false,
      disabled = false,
      placeholder = 'Send a message…',
      keyMode = 'cmd-enter',
      toolbarLeft,
      toolbarRight,
      className,
    },
    ref,
  ) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);

    useImperativeHandle(
      ref,
      () => ({
        focus: () => textareaRef.current?.focus(),
        focusAtEnd: () => {
          const ta = textareaRef.current;
          if (!ta) return;
          ta.focus();
          const len = ta.value.length;
          ta.setSelectionRange(len, len);
        },
      }),
      [],
    );

    // Auto-grow: reset height first so shrink works when user deletes text.
    useEffect(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.style.height = 'auto';
      const next = Math.min(Math.max(ta.scrollHeight, MIN_TEXTAREA_PX), MAX_TEXTAREA_PX);
      ta.style.height = `${next}px`;
    }, [value]);

    const trimmed = value.trim();
    const canSend = trimmed.length > 0 && !disabled && !isStreaming;

    const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.nativeEvent.isComposing) return;
      if (keyMode === 'cmd-enter') {
        const isSendCombo =
          event.key === 'Enter' && (event.metaKey || event.ctrlKey);
        if (isSendCombo) {
          event.preventDefault();
          if (canSend) void onSubmit();
        }
        return;
      }
      // keyMode === 'enter'
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (isStreaming) onCancel?.();
        else if (canSend) void onSubmit();
      }
    };

    const handleSendClick = () => {
      if (isStreaming) {
        onCancel?.();
        return;
      }
      if (canSend) void onSubmit();
    };

    return (
      <div className={cn('chat-input-container px-3 pb-3 pt-1', className)}>
        <div className="chat-input-box flex flex-col rounded-agent-lg border border-agent-border bg-agent-canvas shadow-sm">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            rows={1}
            className="block w-full resize-none border-0 bg-transparent px-3 pt-3 text-sm text-agent-foreground outline-none placeholder:text-agent-muted-foreground disabled:opacity-50"
            style={{ minHeight: MIN_TEXTAREA_PX, maxHeight: MAX_TEXTAREA_PX }}
          />
          <div className="flex items-center justify-between gap-2 px-2 pb-2 pt-1">
            <div className="flex items-center gap-1">{toolbarLeft}</div>
            <div className="flex items-center gap-3">
              {toolbarRight}
              <div className="hidden text-[11px] text-agent-muted-foreground/80 sm:block">
                <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                  {MOD_LABEL}
                </kbd>
                <span className="mx-0.5">+</span>
                <kbd className="rounded border border-agent-border bg-agent-muted px-1 font-sans text-[10px]">
                  {isStreaming ? '.' : 'Enter'}
                </kbd>
              </div>
              <button
                type="button"
                onClick={handleSendClick}
                disabled={!isStreaming && !canSend}
                className={
                  isStreaming
                    ? 'flex h-8 w-8 items-center justify-center rounded-full bg-agent-destructive text-white transition hover:opacity-90'
                    : canSend
                      ? 'flex h-8 w-8 items-center justify-center rounded-full bg-agent-foreground text-agent-canvas transition hover:opacity-90'
                      : 'flex h-8 w-8 cursor-not-allowed items-center justify-center rounded-full bg-agent-muted text-agent-muted-foreground'
                }
                aria-label={isStreaming ? 'Stop generating' : 'Send message'}
                title={
                  isStreaming
                    ? `Stop (${MOD_LABEL}+.)`
                    : `Send (${MOD_LABEL}+Enter)`
                }
              >
                {isStreaming ? (
                  <span className="block h-3.5 w-3.5 bg-white" />
                ) : (
                  <span aria-hidden className="leading-none">↑</span>
                )}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  },
);

// ---------------------------------------------------------------------------
// Empty
// ---------------------------------------------------------------------------

export interface ChatPanelEmptyProps {
  /** Suggested prompts to chip-render. When omitted, the slot is a centred message. */
  prompts?: string[];
  /** Called when the user picks a chip. */
  onSelectPrompt?: (prompt: string) => void;
  title?: string;
  description?: string;
  className?: string;
}

function ChatPanelEmpty(props: ChatPanelEmptyProps) {
  const { prompts, onSelectPrompt, title, description, className } = props;
  return (
    <div className={cn('flex h-full flex-col justify-end gap-3 p-3', className)}>
      {(title || description) && (
        <div className="text-center text-sm text-agent-muted-foreground">
          {title ? (
            <p className="font-medium text-agent-foreground">{title}</p>
          ) : null}
          {description ? <p className="mt-1">{description}</p> : null}
        </div>
      )}
      {Array.isArray(prompts) && prompts.length > 0 ? (
        <div className="flex flex-col items-start gap-2">
          {prompts.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => onSelectPrompt?.(p)}
              title={p}
              className="max-w-full cursor-pointer truncate rounded-full border border-agent-border bg-agent-muted px-3 py-1.5 text-xs text-agent-muted-foreground transition-all duration-200 hover:bg-agent-accent hover:text-agent-foreground"
            >
              {p}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// StreamingStatus
// ---------------------------------------------------------------------------

export interface ChatPanelStreamingStatusProps {
  /** Current round number (1-based). */
  round?: number;
  /** Number of tool calls executed so far in this turn. */
  actionCount?: number;
  /**
   * Suppress the status when the bubble has already received content tokens —
   * the cursor blink in the bubble is enough.
   */
  hasContent?: boolean;
  /** Override the auto-derived label. */
  label?: string;
  className?: string;
}

function selectStatusLabel(round: number, actionCount: number): string {
  if (round >= 2 && actionCount === 0) return `Round ${round} · continuing…`;
  if (actionCount > 0) return `Called ${actionCount} tools, analysing…`;
  return 'Thinking…';
}

function ChatPanelStreamingStatus(props: ChatPanelStreamingStatusProps) {
  const {
    round = 1,
    actionCount = 0,
    hasContent = false,
    label,
    className,
  } = props;
  if (hasContent) return null;
  const text = label ?? selectStatusLabel(round, actionCount);
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'my-2 inline-flex items-center gap-2 text-xs text-agent-muted-foreground',
        className,
      )}
    >
      <span className="inline-flex items-center gap-1">
        <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:0ms]" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:150ms]" />
        <span className="h-1 w-1 animate-pulse rounded-full bg-agent-foreground/40 [animation-delay:300ms]" />
      </span>
      <span>{text}</span>
      {round > 1 ? (
        <span className="rounded-full border border-agent-border bg-agent-muted/40 px-1.5 py-[1px] font-mono text-[10px] text-agent-muted-foreground">
          round {round}
        </span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Backwards-compat monolithic alias
//
// The 0.2.x signature `<ChatPanel messages onSubmit isStreaming … />` keeps
// working — we just wrap it with the compound primitives internally. Existing
// /dev/framework-preview, mock stories, and the deeppath-agent LocalChatPanel
// continue to render without churn.
// ---------------------------------------------------------------------------

export interface ChatPanelProps {
  messages: ChatMessage[];
  isStreaming?: boolean;
  onSubmit: (input: { content: string }) => void | Promise<void>;
  onCancel?: () => void;
  className?: string;
  inputPlaceholder?: string;
  header?: React.ReactNode;
  renderToolCall?: (call: ToolCall, result?: ToolResult) => React.ReactNode;
  renderMessage?: (props: MessageRendererProps) => React.ReactNode;
  emptyState?: React.ReactNode;
  disabled?: boolean;
}

function ChatPanelMonolith(props: ChatPanelProps) {
  const {
    messages,
    isStreaming = false,
    onSubmit,
    onCancel,
    className,
    inputPlaceholder,
    header,
    renderToolCall,
    renderMessage,
    emptyState,
    disabled = false,
  } = props;
  const [draft, setDraft] = useState('');
  const handleSubmit = useCallback(async () => {
    const trimmed = draft.trim();
    if (!trimmed || disabled) return;
    setDraft('');
    await onSubmit({ content: trimmed });
  }, [draft, disabled, onSubmit]);

  return (
    <ChatPanelRoot className={className}>
      {header ? <ChatPanelHeader>{header}</ChatPanelHeader> : null}
      <ChatPanelMessages
        messages={messages}
        isStreaming={isStreaming}
        renderMessage={renderMessage}
        renderToolCall={renderToolCall}
        emptyState={emptyState}
      />
      <ChatPanelInput
        value={draft}
        onChange={setDraft}
        onSubmit={handleSubmit}
        onCancel={onCancel}
        isStreaming={isStreaming}
        disabled={disabled}
        placeholder={inputPlaceholder}
        keyMode="enter"
      />
    </ChatPanelRoot>
  );
}

// ---------------------------------------------------------------------------
// Connected
//
// The wave-4 deliverable: a `ChatPanel.Connected` shortcut that reads from a
// `ChatSessionProvider` mounted above, so an embedding looks like:
//
//   <ChatSessionProvider value={useChatSession({ transport })}>
//     <ChatPanel.Connected emptyPrompts={['Hello', 'Resume']} />
//   </ChatSessionProvider>
//
// Apps that haven't migrated to a provider continue to pass props explicitly
// via the compound parts.
// ---------------------------------------------------------------------------

export interface ChatPanelConnectedProps {
  className?: string;
  header?: React.ReactNode;
  emptyPrompts?: string[];
  emptyTitle?: string;
  emptyDescription?: string;
  inputPlaceholder?: string;
  renderMessage?: (props: MessageRendererProps) => React.ReactNode;
  renderToolCall?: (call: ToolCall, result?: ToolResult) => React.ReactNode;
  /** Apply the panel's own background/foreground tokens (default). */
  styled?: boolean;
}

function ChatPanelConnected(props: ChatPanelConnectedProps) {
  const {
    className,
    header,
    emptyPrompts,
    emptyTitle,
    emptyDescription,
    inputPlaceholder,
    renderMessage,
    renderToolCall,
    styled = true,
  } = props;
  const session = useChatSessionContext();
  const { composer } = session;

  const emptyState =
    emptyPrompts || emptyTitle || emptyDescription ? (
      <ChatPanelEmpty
        prompts={emptyPrompts}
        title={emptyTitle}
        description={emptyDescription}
        onSelectPrompt={(text) => {
          composer.setValue(text);
          void composer.submit();
        }}
      />
    ) : undefined;

  return (
    <ChatPanelRoot className={className} unstyled={!styled}>
      {header ? <ChatPanelHeader>{header}</ChatPanelHeader> : null}
      <ChatPanelMessages
        messages={session.messages}
        isStreaming={session.isStreaming}
        renderMessage={renderMessage}
        renderToolCall={renderToolCall}
        emptyState={emptyState}
      />
      <ChatPanelInput
        value={composer.value}
        onChange={composer.setValue}
        onSubmit={composer.submit}
        onCancel={composer.cancel}
        isStreaming={session.isStreaming}
        placeholder={inputPlaceholder}
      />
    </ChatPanelRoot>
  );
}

// `ChatPanel` is the monolith (backwards-compat); the compound parts hang
// off it as named attributes. This mirrors Radix's pattern so consumers can
// keep `<ChatPanel ...>` working OR migrate to `<ChatPanel.Root>…</ChatPanel.Root>`
// in their own time.
const ChatPanelCompound = Object.assign(ChatPanelMonolith, {
  Root: ChatPanelRoot,
  Header: ChatPanelHeader,
  Messages: ChatPanelMessages,
  Input: ChatPanelInput,
  Empty: ChatPanelEmpty,
  StreamingStatus: ChatPanelStreamingStatus,
  Connected: ChatPanelConnected,
});

export { ChatPanelCompound as ChatPanel };
export type { MessageRendererProps };
