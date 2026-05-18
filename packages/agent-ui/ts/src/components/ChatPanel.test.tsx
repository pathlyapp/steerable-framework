/**
 * Tests for the compound `ChatPanel` API.
 *
 * The structural test surface is small — the heavy lifting is in MessageList
 * and the state hooks — but we want to lock in the contract that the
 * compound parts hang off `ChatPanel` AND that the monolithic alias still
 * renders messages + a draftable input.
 */

import { describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { ChatPanel } from './ChatPanel';

const NOW = '2026-05-15T00:00:00Z';
const sample: ChatMessage[] = [
  { id: 'u1', role: 'user', content: 'hello', createdAt: NOW },
  { id: 'a1', role: 'assistant', content: 'hi there', createdAt: NOW },
];

function CompoundDemo({ onSubmit }: { onSubmit: () => void }) {
  const [draft, setDraft] = useState('');
  return (
    <ChatPanel.Root>
      <ChatPanel.Messages messages={sample} />
      <ChatPanel.Input
        value={draft}
        onChange={setDraft}
        onSubmit={() => {
          onSubmit();
          setDraft('');
        }}
      />
    </ChatPanel.Root>
  );
}

describe('ChatPanel compound', () => {
  it('exposes Root, Header, Messages, Input, Empty, StreamingStatus as attached subs', () => {
    // forwardRef components are objects with a render fn; plain components
    // are functions. We assert "callable-ish" rather than pinning the kind.
    for (const sub of [
      ChatPanel.Root,
      ChatPanel.Header,
      ChatPanel.Messages,
      ChatPanel.Input,
      ChatPanel.Empty,
      ChatPanel.StreamingStatus,
    ]) {
      expect(sub).toBeDefined();
    }
  });

  it('Root + Messages + Input compose into a working shell', () => {
    render(<CompoundDemo onSubmit={() => {}} />);
    expect(screen.getByText('hello')).toBeTruthy();
    expect(screen.getByText('hi there')).toBeTruthy();
  });

  it('Empty slot renders prompts and fires onSelectPrompt on click', () => {
    const onSelectPrompt = vi.fn();
    render(
      <ChatPanel.Empty
        title="Try a prompt"
        prompts={['plan my day', 'summarise']}
        onSelectPrompt={onSelectPrompt}
      />,
    );
    expect(screen.getByText('Try a prompt')).toBeTruthy();
    fireEvent.click(screen.getByText('plan my day'));
    expect(onSelectPrompt).toHaveBeenCalledWith('plan my day');
  });

  it('StreamingStatus hides when hasContent=true and renders text otherwise', () => {
    const { rerender, queryByText } = render(
      <ChatPanel.StreamingStatus hasContent={true} round={1} actionCount={0} />,
    );
    expect(queryByText('Thinking…')).toBeNull();
    rerender(
      <ChatPanel.StreamingStatus hasContent={false} round={1} actionCount={0} />,
    );
    expect(queryByText('Thinking…')).toBeTruthy();
  });

  it('monolithic alias still renders messages + accepts onSubmit prop', () => {
    const onSubmit = vi.fn();
    render(<ChatPanel messages={sample} onSubmit={onSubmit} />);
    expect(screen.getByText('hello')).toBeTruthy();
    expect(screen.getByText('hi there')).toBeTruthy();
  });

  it('Input with keyMode=enter sends on plain Enter and inserts newline on Shift+Enter', () => {
    const onSubmit = vi.fn();
    function Demo() {
      const [draft, setDraft] = useState('ping');
      return (
        <ChatPanel.Input
          value={draft}
          onChange={setDraft}
          onSubmit={() => onSubmit()}
          keyMode="enter"
        />
      );
    }
    render(<Demo />);
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(onSubmit).toHaveBeenCalledOnce();
    onSubmit.mockClear();
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('Input toggles to a Stop button while streaming, fires onCancel on click', () => {
    const onCancel = vi.fn();
    render(
      <ChatPanel.Input
        value=""
        onChange={() => {}}
        onSubmit={() => {}}
        onCancel={onCancel}
        isStreaming
      />,
    );
    const stopBtn = screen.getByLabelText('Stop generating');
    fireEvent.click(stopBtn);
    expect(onCancel).toHaveBeenCalledOnce();
  });
});
