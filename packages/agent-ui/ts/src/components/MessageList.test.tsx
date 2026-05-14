import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '@steerable/agent-protocol';
import { MessageList } from './MessageList';

const baseMessages: ChatMessage[] = [
  {
    id: 'u1',
    role: 'user',
    content: 'Hi there!',
    createdAt: new Date('2025-01-01T00:00:00Z').toISOString(),
  },
  {
    id: 'a1',
    role: 'assistant',
    content: 'Hello! How can I help?',
    createdAt: new Date('2025-01-01T00:00:01Z').toISOString(),
  },
];

describe('MessageList', () => {
  it('renders each message with the role data-attribute', () => {
    render(<MessageList messages={baseMessages} />);
    expect(screen.getByText('Hi there!')).toBeTruthy();
    expect(screen.getByText('Hello! How can I help?')).toBeTruthy();
    const roles = Array.from(
      document.querySelectorAll('[data-role]'),
    ).map((el) => el.getAttribute('data-role'));
    expect(roles).toEqual(['user', 'assistant']);
  });

  it('renders the empty state slot when no messages and one is provided', () => {
    render(
      <MessageList
        messages={[]}
        emptyState={<div>start a conversation</div>}
      />,
    );
    expect(screen.getByText('start a conversation')).toBeTruthy();
  });

  it('uses the supplied renderMessage when provided', () => {
    render(
      <MessageList
        messages={baseMessages}
        renderMessage={({ message }) => (
          <span data-testid="custom">CUSTOM:{message.content}</span>
        )}
      />,
    );
    const items = screen.getAllByTestId('custom');
    expect(items.map((el) => el.textContent)).toEqual([
      'CUSTOM:Hi there!',
      'CUSTOM:Hello! How can I help?',
    ]);
  });
});
