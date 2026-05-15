import type { Meta, StoryObj } from '@storybook/react';
import type { ChatMessage } from '@steerable/agent-protocol';
import { MessageList } from './MessageList.js';

const meta: Meta<typeof MessageList> = {
  title: 'Components/MessageList',
  component: MessageList,
  parameters: {
    layout: 'centered',
    docs: {
      description: {
        component:
          'Headless message list with auto-scroll-to-bottom. Pass a `renderMessage` prop to swap in your own bubble. The default renderer just shows role + content and is meant for sandbox / debugging only.',
      },
    },
  },
  decorators: [
    (Story) => (
      <div className="h-[480px] w-[640px] border border-agent-border bg-agent-canvas">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof MessageList>;

const NOW = '2026-05-15T00:00:00Z';

const baseMessages: ChatMessage[] = [
  { id: '1', role: 'user', content: 'Hi! What can you help me with?', createdAt: NOW },
  {
    id: '2',
    role: 'assistant',
    content:
      'I can plan trips, draft emails, query your knowledge base and run safe shell commands. What would you like to start with?',
    createdAt: NOW,
  },
  { id: '3', role: 'user', content: 'Plan a 2-day weekend in Kyoto.', createdAt: NOW },
  {
    id: '4',
    role: 'assistant',
    content:
      "Sure — Day 1: Fushimi Inari at sunrise, then Gion in the afternoon. Day 2: Arashiyama bamboo grove and a kaiseki dinner. I'll add restaurant picks next.",
    createdAt: NOW,
  },
];

export const Default: Story = {
  args: {
    messages: baseMessages,
  },
};

export const Empty: Story = {
  args: {
    messages: [],
  },
};

export const EmptyWithCustomState: Story = {
  args: {
    messages: [],
    emptyState: (
      <div className="m-auto max-w-xs text-center text-sm text-agent-muted-foreground">
        <p className="font-medium text-agent-foreground">Nothing to show yet</p>
        <p className="mt-1">Send your first message to start a conversation.</p>
      </div>
    ),
  },
};

export const Streaming: Story = {
  args: {
    messages: [
      ...baseMessages,
      {
        id: '5',
        role: 'assistant',
        content: "Day 2 — Morning: rent a bike near Arashiyama station and",
        createdAt: NOW,
      },
    ],
    isStreaming: true,
  },
};

export const StreamingEmptyAssistant: Story = {
  args: {
    messages: [
      ...baseMessages,
      { id: '6', role: 'assistant', content: '', createdAt: NOW },
    ],
    isStreaming: true,
  },
};

export const LongConversation: Story = {
  args: {
    messages: Array.from({ length: 30 }, (_, i): ChatMessage => ({
      id: `m-${i}`,
      role: i % 2 === 0 ? 'user' : 'assistant',
      content:
        i % 2 === 0
          ? `User question #${i / 2 + 1}: tell me about topic ${i}.`
          : `Assistant answer to question #${(i - 1) / 2 + 1}. ` +
            'This response wraps across multiple lines so we exercise the auto-scroll behaviour and verify the bubble shape under longer content.',
      createdAt: NOW,
    })),
  },
};
