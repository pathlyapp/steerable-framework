/**
 * Storybook story for `MockChatStreamTransport` paired with `useChatSession`.
 * This is the canonical "framework runs end-to-end with zero backend" demo —
 * the same pattern is used by `examples/web-shell` in wave 5.
 */

import type { Meta, StoryObj } from '@storybook/react';
import { useMemo } from 'react';
import type { SSEEvent } from '@steerable/agent-protocol';
import { ChatPanel } from '../components/ChatPanel.js';
import { MockChatStreamTransport } from './MockChatStreamTransport.js';
import { useChatSession } from './useChatSession.js';

const meta: Meta = {
  title: 'State/MockChatStreamTransport',
  parameters: {
    layout: 'fullscreen',
    docs: {
      description: {
        component:
          'Scripted SSE transport used in tests, stories, and the framework web-shell example. Pair with `useChatSession` for a fully offline chat demo.',
      },
    },
  },
};
export default meta;
type Story = StoryObj;

const baselineScript: SSEEvent[] = [
  { type: 'content', content: 'Sure — ' },
  { type: 'content', content: "here's a plan:\n\n" },
  { type: 'content', content: '1. Outline goals\n' },
  { type: 'content', content: '2. Gather inputs\n' },
  { type: 'content', content: '3. Iterate\n\n' },
  { type: 'content', content: 'Want me to dive deeper into any step?' },
  { type: 'done' },
];

const toolCallScript: SSEEvent[] = [
  { type: 'content', content: 'Checking the weather…\n' },
  {
    type: 'tool_call',
    payload: { id: 'c1', name: 'get_weather', arguments: { city: 'Tokyo' } },
  },
  {
    type: 'tool_result',
    payload: { success: true, data: { temp: 22, conditions: 'clear' } },
  },
  { type: 'content', content: '\nIt is 22°C and clear in Tokyo right now.' },
  { type: 'done' },
];

function DemoChat({ scripts, delayMs }: { scripts: SSEEvent[][]; delayMs: number }) {
  const transport = useMemo(
    () =>
      new MockChatStreamTransport({
        scripts,
        defaultDelayMs: delayMs,
        exhaustionPolicy: 'cycle',
      }),
    [scripts, delayMs],
  );
  const session = useChatSession({ transport });

  return (
    <div className="h-[640px] w-full max-w-3xl border border-agent-border bg-agent-canvas">
      <ChatPanel
        messages={session.messages}
        isStreaming={session.isStreaming}
        onSubmit={async ({ content }) => {
          await session.sendUserMessage({ content });
        }}
        onCancel={session.cancel}
        emptyState={
          <div className="m-auto max-w-sm text-center text-sm text-agent-muted-foreground">
            <p className="font-medium text-agent-foreground">Try the mock transport</p>
            <p className="mt-1">Anything you type triggers the next scripted reply.</p>
          </div>
        }
      />
    </div>
  );
}

export const ContentOnly: Story = {
  render: () => <DemoChat scripts={[baselineScript]} delayMs={30} />,
};

export const WithToolCall: Story = {
  render: () => <DemoChat scripts={[toolCallScript]} delayMs={30} />,
};

export const Cycling: Story = {
  render: () => <DemoChat scripts={[baselineScript, toolCallScript]} delayMs={30} />,
};
