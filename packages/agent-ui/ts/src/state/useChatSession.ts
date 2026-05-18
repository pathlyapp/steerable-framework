/**
 * `useChatSession` — convenience wrapper that combines `useChatStream` (turn
 * lifecycle) with `useChatComposer` (draft + keys + send). Most consumers want
 * this single hook so the wiring is identical across web-shell, deeppath, and
 * deeppath-agent. Power users still reach for the underlying primitives.
 *
 *   const { messages, isStreaming, composer, cancel } = useChatSession({
 *     transport,
 *     initialMessages,
 *   });
 *
 *   <ChatPanel>
 *     <MessageList messages={messages} isStreaming={isStreaming} />
 *     <ChatInput composer={composer} onCancel={cancel} />
 *   </ChatPanel>
 */

import { useMemo } from 'react';
import {
  useChatStream,
  type UseChatStreamOptions,
  type UseChatStreamReturn,
} from '../hooks/useChatStream.js';
import {
  useChatComposer,
  type UseChatComposerOptions,
  type UseChatComposerReturn,
} from './useChatComposer.js';

export interface UseChatSessionOptions
  extends UseChatStreamOptions,
    Pick<UseChatComposerOptions, 'enterToSubmit' | 'initialValue'> {
  /** Optional metadata callback when submitting a message (e.g. @mentions). */
  buildMetadata?: (value: string) => Record<string, unknown> | undefined;
  /** Force-disable the composer regardless of streaming state. */
  composerDisabled?: boolean;
}

export interface UseChatSessionReturn extends UseChatStreamReturn {
  composer: UseChatComposerReturn;
}

export function useChatSession(options: UseChatSessionOptions): UseChatSessionReturn {
  const stream = useChatStream(options);

  const composer = useChatComposer({
    initialValue: options.initialValue,
    enterToSubmit: options.enterToSubmit,
    isStreaming: stream.isStreaming,
    disabled: options.composerDisabled,
    onSend: async (value) => {
      await stream.sendUserMessage({
        content: value,
        metadata: options.buildMetadata?.(value),
      });
    },
    onCancel: stream.cancel,
  });

  return useMemo(
    () => ({ ...stream, composer }),
    [stream, composer],
  );
}
