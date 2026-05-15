import type { Meta, StoryObj } from '@storybook/react';
import type { SSEEvent } from '@steerable/agent-protocol';
import { SSEStreamView } from './SSEStreamView.js';

const meta: Meta<typeof SSEStreamView> = {
  title: 'Components/SSEStreamView',
  component: SSEStreamView,
  parameters: {
    layout: 'centered',
    docs: {
      description: {
        component:
          'Append-only debug view of the raw SSE event log. Used in production for the "Trace" tab in `deeppath/apps/web` and as a dev console inside `deeppath-agent`. Toggle `verbose` to inspect full JSON payloads, or pass `filterTypes` to narrow to a single event family.',
      },
    },
  },
  decorators: [
    (Story) => (
      <div className="h-[400px] w-[640px]">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof SSEStreamView>;

const events: SSEEvent[] = [
  { type: 'agent', agentId: 'planner', model: 'gpt-4o-mini' },
  {
    type: 'orchestration',
    orchestrationGroupId: 'plan-1',
    payload: { phase: 'plan_started' },
  },
  { type: 'content', content: 'Looking up the latest project status…' },
  {
    type: 'tool_call',
    payload: {
      callId: 'call_1',
      name: 'project_search',
      arguments: { query: 'Q4 roadmap', limit: 5 },
    },
  },
  {
    type: 'tool_result',
    payload: { callId: 'call_1', name: 'project_search', status: 'success', count: 3 },
  },
  { type: 'content', content: 'Found 3 active projects. Drafting summary…' },
  { type: 'loader-hint', hint: 'Composing response…' },
  { type: 'content', content: 'Here are the highlights:\n1. Project Alpha\n2. Project Beta' },
  { type: 'done' },
];

export const Empty: Story = {
  args: {
    events: [],
  },
};

export const Default: Story = {
  args: {
    events,
  },
};

export const Verbose: Story = {
  args: {
    events,
    verbose: true,
  },
};

export const FilteredToolEvents: Story = {
  args: {
    events,
    filterTypes: ['tool_call', 'tool_result'],
  },
};

export const ErrorAndBudget: Story = {
  args: {
    events: [
      ...events.slice(0, 5),
      { type: 'budget_exhausted', message: 'token budget reached after 3.2k tokens' },
      { type: 'error', message: 'tool_dispatch failed: timeout after 30s' },
    ],
  },
};

export const HighVolume: Story = {
  args: {
    events: Array.from({ length: 200 }, (_, i): SSEEvent => {
      if (i % 13 === 0) return { type: 'tool_call', payload: { callId: `c${i}`, name: 'noop' } };
      if (i % 13 === 6) return { type: 'tool_result', payload: { callId: `c${i - 6}`, status: 'success' } };
      return { type: 'content', content: `chunk ${i}: ` + 'lorem ipsum dolor sit amet '.repeat(2) };
    }),
  },
};
