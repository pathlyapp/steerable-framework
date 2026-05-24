/**
 * Smoke tests for `ChatSessionProvider`. Verifies that the provider exposes
 * the active session, that `useChatSessionContext` throws outside it, and
 * that the optional / slice variants degrade gracefully.
 */
import * as React from 'react';
import { render, screen, act, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  ChatSessionProvider,
  useChatSessionContext,
  useOptionalChatSession,
  useChatSessionSlice,
  useChatSession,
} from './index.js';
import { MockChatStreamTransport } from './MockChatStreamTransport.js';

function Probe({ onValue }: { onValue: (v: unknown) => void }) {
  const ctx = useChatSessionContext();
  React.useEffect(() => {
    onValue({ messages: ctx.messages.length, isStreaming: ctx.isStreaming });
  }, [ctx.messages.length, ctx.isStreaming, onValue]);
  return <span data-testid="ok">ok</span>;
}

function OptionalProbe({ onResult }: { onResult: (v: unknown) => void }) {
  const ctx = useOptionalChatSession();
  React.useEffect(() => onResult(ctx), [ctx, onResult]);
  return <span>maybe</span>;
}

function SliceProbe({ onValue }: { onValue: (v: unknown) => void }) {
  const slice = useChatSessionSlice();
  React.useEffect(() => onValue(slice), [slice, onValue]);
  return <span>slice</span>;
}

function Host({ children }: { children: React.ReactNode }) {
  const transport = React.useMemo(
    () =>
      new MockChatStreamTransport({
        scripts: [
          [
            { event: { type: 'content', content: 'hi' } },
            { event: { type: 'done' } },
          ],
        ],
      }),
    [],
  );
  const session = useChatSession({ transport });
  return <ChatSessionProvider value={session}>{children}</ChatSessionProvider>;
}

describe('ChatSessionProvider', () => {
  it('exposes the session via useChatSessionContext', () => {
    const seen = vi.fn();
    render(
      <Host>
        <Probe onValue={seen} />
      </Host>,
    );
    expect(screen.getByTestId('ok')).toBeTruthy();
    expect(seen).toHaveBeenCalled();
  });

  it('useChatSessionContext throws outside provider', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(() => {
      render(<Probe onValue={vi.fn()} />);
    }).toThrow(/inside a <ChatSessionProvider/);
    consoleError.mockRestore();
  });

  it('useOptionalChatSession returns null outside provider', () => {
    const seen = vi.fn();
    render(<OptionalProbe onResult={seen} />);
    expect(seen).toHaveBeenCalledWith(null);
  });

  it('useChatSessionSlice returns null outside provider', () => {
    const seen = vi.fn();
    render(<SliceProbe onValue={seen} />);
    expect(seen).toHaveBeenCalledWith(null);
  });
});
