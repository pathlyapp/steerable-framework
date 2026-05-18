import type { Meta, StoryObj } from '@storybook/react';
import { useState } from 'react';
import type { ChatAgent } from '@steerable/agent-protocol';
import { AgentSelector } from './AgentSelector.js';

const NOW = '2026-05-16T00:00:00Z';

const baseAgents: ChatAgent[] = [
  {
    id: 'local-assistant',
    name: 'Local Assistant',
    icon: '🤖',
    createdAt: NOW,
    updatedAt: NOW,
  },
  {
    id: 'researcher',
    name: 'Researcher',
    icon: '🔎',
    createdAt: NOW,
    updatedAt: NOW,
  },
  {
    id: 'planner',
    name: 'Planner',
    icon: '🧭',
    createdAt: NOW,
    updatedAt: NOW,
  },
];

const meta: Meta<typeof AgentSelector> = {
  title: 'Components/AgentSelector',
  component: AgentSelector,
  parameters: {
    layout: 'centered',
    actions: { argTypesRegex: '^on[A-Z].*' },
    docs: {
      description: {
        component:
          'Segmented selector for choosing the active chat agent. Supports keyboard navigation and custom rendering via `renderAgent`.',
      },
    },
  },
  args: {
    agents: baseAgents,
    selectedId: baseAgents[0].id,
    onSelect: () => {},
  },
};

export default meta;
type Story = StoryObj<typeof AgentSelector>;

export const Default: Story = {};

export const Disabled: Story = {
  args: {
    disabled: true,
  },
};

export const SingleAgent: Story = {
  args: {
    agents: [baseAgents[0]],
    selectedId: baseAgents[0].id,
  },
};

export const LongNames: Story = {
  args: {
    agents: [
      {
        id: 'very-long',
        name: 'This is a very long agent name that should truncate gracefully',
        icon: '🧪',
        createdAt: NOW,
        updatedAt: NOW,
      },
      {
        id: 'also-long',
        name: 'Another very long and descriptive specialist agent name',
        icon: '🛠️',
        createdAt: NOW,
        updatedAt: NOW,
      },
    ],
    selectedId: 'very-long',
  },
};

export const Loading: Story = {
  args: {
    loading: true,
    agents: [],
  },
};

export const Interactive: Story = {
  render: (args) => {
    const InteractiveSelector = () => {
      const [selectedId, setSelectedId] = useState(args.selectedId);
      return (
        <AgentSelector
          {...args}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      );
    };
    return <InteractiveSelector />;
  },
};
