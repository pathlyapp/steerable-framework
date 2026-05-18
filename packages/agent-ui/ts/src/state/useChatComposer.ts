/**
 * `useChatComposer` — input draft + send/cancel + keyboard glue.
 *
 * Lifted from deeppath-agent's `LocalChatPanel.tsx` (inline `inputValue` /
 * `handleSubmit`) and deeppath's `ChatInput` (cancel handling). The hook is
 * intentionally UI-free: it owns the draft string and the keyboard
 * dispatcher; the visible textarea and send button live in the consuming
 * component.
 *
 * Typical wiring:
 *
 *   const composer = useChatComposer({ onSend: send, onCancel: cancel });
 *
 *   <textarea
 *     value={composer.value}
 *     onChange={(e) => composer.setValue(e.target.value)}
 *     onKeyDown={composer.handleKeyDown}
 *   />
 *   <button onClick={composer.submit}>Send</button>
 */

import { useCallback, useRef, useState } from 'react';

export interface UseChatComposerOptions {
  initialValue?: string;
  /** Called with the trimmed value when the user submits a non-empty draft. */
  onSend: (value: string) => void | Promise<void>;
  /** Called when the user wants to cancel the in-flight stream. Optional. */
  onCancel?: () => void;
  /**
   * If true (default), Enter submits and Shift+Enter inserts a newline. Set
   * to false for "Enter inserts newline, Cmd/Ctrl+Enter submits" semantics.
   */
  enterToSubmit?: boolean;
  /** Whether the composer is currently disabled (no submit / no key handling). */
  disabled?: boolean;
  /**
   * Whether the parent reports an in-flight stream. When true, `submit()` is
   * a no-op and Enter triggers `onCancel` instead. Mirrors the cloud product.
   */
  isStreaming?: boolean;
}

export interface UseChatComposerReturn {
  value: string;
  setValue: (value: string) => void;
  submit: () => Promise<void> | void;
  cancel: () => void;
  clear: () => void;
  handleKeyDown: (e: React.KeyboardEvent<HTMLElement>) => void;
  /** True if `value.trim()` is non-empty and we're not disabled. */
  canSubmit: boolean;
}

export function useChatComposer(options: UseChatComposerOptions): UseChatComposerReturn {
  const [value, setValue] = useState(options.initialValue ?? '');
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const submit = useCallback(async () => {
    const opts = optionsRef.current;
    if (opts.disabled || opts.isStreaming) return;
    const trimmed = value.trim();
    if (!trimmed) return;
    // Clear synchronously so the user can keep typing while the request flies.
    setValue('');
    await opts.onSend(trimmed);
  }, [value]);

  const cancel = useCallback(() => {
    optionsRef.current.onCancel?.();
  }, []);

  const clear = useCallback(() => setValue(''), []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      const opts = optionsRef.current;
      if (opts.disabled) return;
      const enterSubmits = opts.enterToSubmit ?? true;
      if (e.key === 'Enter') {
        if (enterSubmits && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
          e.preventDefault();
          if (opts.isStreaming) {
            cancel();
          } else {
            void submit();
          }
          return;
        }
        if (!enterSubmits && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          if (opts.isStreaming) cancel();
          else void submit();
          return;
        }
      }
      if (e.key === 'Escape' && opts.isStreaming) {
        e.preventDefault();
        cancel();
      }
    },
    [submit, cancel],
  );

  const canSubmit = value.trim().length > 0 && !(options.disabled || options.isStreaming);

  return {
    value,
    setValue,
    submit,
    cancel,
    clear,
    handleKeyDown,
    canSubmit,
  };
}
