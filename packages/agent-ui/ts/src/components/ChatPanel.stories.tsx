import type { Meta, StoryObj } from '@storybook/react';
import { useState } from 'react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { ChatPanel } from './ChatPanel.js';

const meta: Meta<typeof ChatPanel> = {
  title: 'Components/ChatPanel',
  component: ChatPanel,
  parameters: {
    layout: 'fullscreen',
    actions: { argTypesRegex: '^on[A-Z].*' },
    docs: {
      description: {
        component:
          'Foundational orchestration shell. Renders a `MessageList` plus a minimal input row and a stop button. Production callers typically wrap it with their own header/composer and only feed in `messages` + `onSubmit`.',
      },
    },
  },
  args: {
    onSubmit: () => {},
    inputPlaceholder: 'Send a message…',
  },
  decorators: [
    (Story) => (
      <div className="h-[640px] w-full max-w-3xl border border-agent-border bg-agent-canvas">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof ChatPanel>;

const NOW = '2026-05-15T00:00:00Z';

const sampleMessages: ChatMessage[] = [
  {
    id: 'm-1',
    role: 'user',
    content: 'Plan a 3-day trip to Tokyo for a first-time visitor.',
    createdAt: NOW,
  },
  {
    id: 'm-2',
    role: 'assistant',
    content:
      "Sure — here's a balanced 3-day plan covering culture, food and a half-day trip outside the city. I'll list each day with morning/afternoon/evening blocks.",
    createdAt: NOW,
  },
  {
    id: 'm-3',
    role: 'user',
    content: 'Great. Also include a vegetarian dinner each night.',
    createdAt: NOW,
  },
];

export const Empty: Story = {
  args: {
    messages: [],
    emptyState: (
      <div className="m-auto max-w-sm text-center text-sm text-agent-muted-foreground">
        <p className="font-medium text-agent-foreground">Start a new conversation</p>
        <p className="mt-1">Ask anything — I'll plan, search and call tools as needed.</p>
      </div>
    ),
  },
};

export const WithMessages: Story = {
  args: {
    messages: sampleMessages,
  },
};

export const Streaming: Story = {
  args: {
    messages: [
      ...sampleMessages,
      {
        id: 'm-4',
        role: 'assistant',
        content: "Day 1 — Asakusa & Akihabara. Morning: visit Sensō-ji",
        createdAt: NOW,
      },
    ],
    isStreaming: true,
  },
};

export const Disabled: Story = {
  args: {
    messages: sampleMessages,
    disabled: true,
    inputPlaceholder: 'Loading conversation…',
  },
};

export const WithToolCalls: Story = {
  args: {
    messages: [
      sampleMessages[0],
      {
        id: 'm-tc',
        role: 'assistant',
        content: 'Looking up current restaurant options in Tokyo for you.',
        createdAt: NOW,
        toolCalls: [
          {
            id: 'call-1',
            name: 'web_search',
            arguments: { query: 'best vegetarian restaurants Shibuya 2026' },
          },
        ],
        toolResult: {
          success: true,
          message: 'Found 2 results.',
          data: {
            hits: [
              { title: 'T\u2019s Tantan (Shibuya)', url: 'https://example.com' },
              { title: 'AIN SOPH. Soar', url: 'https://example.com' },
            ],
          },
        },
      },
    ],
  },
};

/**
 * Demonstrates the full controlled pattern: parent owns the message list,
 * appends a new turn on submit, and toggles `isStreaming` while the assistant
 * is "responding". Useful for sandbox-testing custom renderers.
 */
export const Interactive: Story = {
  args: {
    messages: sampleMessages,
  },
  render: (args) => {
    const InteractiveChat = () => {
      const [messages, setMessages] = useState<ChatMessage[]>(args.messages);
      const [streaming, setStreaming] = useState(false);

      return (
        <ChatPanel
          {...args}
          messages={messages}
          isStreaming={streaming}
          onSubmit={async ({ content }) => {
            setMessages((prev) => [
              ...prev,
              { id: `u-${prev.length}`, role: 'user', content, createdAt: new Date().toISOString() },
            ]);
            setStreaming(true);
            await new Promise((r) => setTimeout(r, 600));
            setMessages((prev) => [
              ...prev,
              {
                id: `a-${prev.length}`,
                role: 'assistant',
                content: `Echo: ${content}`,
                createdAt: new Date().toISOString(),
              },
            ]);
            setStreaming(false);
          }}
        />
      );
    };
    return <InteractiveChat />;
  },
};
