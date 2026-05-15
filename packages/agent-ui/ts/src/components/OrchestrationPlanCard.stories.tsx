import type { Meta, StoryObj } from '@storybook/react';
import { OrchestrationPlanCard, type OrchestrationStep } from './OrchestrationPlanCard.js';

const meta: Meta<typeof OrchestrationPlanCard> = {
  title: 'Components/OrchestrationPlanCard',
  component: OrchestrationPlanCard,
  parameters: {
    layout: 'centered',
    actions: { argTypesRegex: '^on[A-Z].*' },
    docs: {
      description: {
        component:
          'Read-only multi-step plan with per-step status icons and a header summary. Status values are `pending` / `running` / `done` / `error` / `skipped`. Pass `onStepClick` to make rows interactive (used in production to open a step\u2019s trace timeline in a side panel).',
      },
    },
  },
  args: {
    title: 'Plan',
  },
  decorators: [
    (Story) => (
      <div className="w-[480px]">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof OrchestrationPlanCard>;

const steps: OrchestrationStep[] = [
  { id: 's1', title: 'Parse user request', status: 'done', kind: 'decision' },
  {
    id: 's2',
    title: 'Look up matching projects',
    status: 'done',
    kind: 'tool',
    description: 'web_search → project_index',
  },
  {
    id: 's3',
    title: 'Draft proposal outline',
    status: 'running',
    kind: 'agent:writer',
  },
  { id: 's4', title: 'Estimate budget', status: 'pending', kind: 'tool' },
  { id: 's5', title: 'Send for review', status: 'pending', kind: 'wait' },
];

export const InProgress: Story = {
  args: {
    steps,
  },
};

export const AllDone: Story = {
  args: {
    steps: steps.map((s) => ({ ...s, status: 'done' as const, finishedAt: '15:32' })),
  },
};

export const WithError: Story = {
  args: {
    steps: [
      ...steps.slice(0, 2),
      {
        ...steps[2],
        status: 'error',
        description: 'writer_agent rejected: missing required tone parameter',
      },
      ...steps.slice(3),
    ],
  },
};

export const SkippedBranch: Story = {
  args: {
    steps: [
      ...steps.slice(0, 3).map((s) => ({ ...s, status: 'done' as const })),
      { ...steps[3], status: 'skipped' as const, description: 'no budget needed' },
      { ...steps[4], status: 'done' as const, finishedAt: '15:55' },
    ],
  },
};

export const Empty: Story = {
  args: {
    steps: [],
  },
};

export const Interactive: Story = {
  args: {
    steps,
    onStepClick: () => {},
  },
};
