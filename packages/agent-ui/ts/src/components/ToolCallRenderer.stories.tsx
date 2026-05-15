import type { Meta, StoryObj } from '@storybook/react';
import type { ToolCall, ToolResult } from '@steerable/agent-protocol';
import { ToolCallRenderer } from './ToolCallRenderer.js';

const meta: Meta<typeof ToolCallRenderer> = {
  title: 'Components/ToolCallRenderer',
  component: ToolCallRenderer,
  parameters: {
    layout: 'centered',
    actions: { argTypesRegex: '^on[A-Z].*' },
    docs: {
      description: {
        component:
          'Single tool-call card. Computes its display state from `useToolCallStatus` (call + optional result + mode hint). Pass `mode="local"` and `onApprove` / `onReject` to render an approval prompt for local-execution tools.',
      },
    },
  },
  decorators: [
    (Story) => (
      <div className="w-[520px]">
        <Story />
      </div>
    ),
  ],
};

export default meta;
type Story = StoryObj<typeof ToolCallRenderer>;

const readCall: ToolCall = {
  id: 'call_read_1',
  name: 'web_search',
  arguments: { query: 'tokyo vegetarian restaurants 2026' },
};

const writeCall: ToolCall = {
  id: 'call_write_1',
  name: 'create_calendar_event',
  arguments: {
    title: 'Team retro',
    startTime: '2026-05-20T14:00:00Z',
    durationMinutes: 60,
  },
};

const destructiveCall: ToolCall = {
  id: 'call_destructive_1',
  name: 'shell_exec',
  arguments: { command: 'rm -rf /tmp/build-cache' },
};

const localCall: ToolCall = {
  id: 'call_local_1',
  name: 'cflog_open_workspace',
  arguments: { path: '/Users/me/projects/cflog/case-2024-001' },
};

const successResult: ToolResult = {
  success: true,
  message: 'Found 12 results.',
  data: {
    hits: [
      { title: 'T\u2019s Tantan', url: 'https://example.com/1' },
      { title: 'AIN SOPH. Soar', url: 'https://example.com/2' },
    ],
  },
};

const errorResult: ToolResult = {
  success: false,
  error: 'TimeRangeConflict: another event already occupies 14:00–15:00 on 2026-05-20',
};

export const ReadModePending: Story = {
  args: {
    call: readCall,
    mode: 'read',
  },
};

export const ReadModeSuccess: Story = {
  args: {
    call: readCall,
    result: successResult,
    mode: 'read',
    defaultExpanded: true,
  },
};

export const SafeWriteRunning: Story = {
  args: {
    call: writeCall,
    mode: 'safe_write',
    runningHint: true,
  },
};

export const SafeWriteError: Story = {
  args: {
    call: writeCall,
    result: errorResult,
    mode: 'safe_write',
    defaultExpanded: true,
  },
};

export const DestructivePending: Story = {
  args: {
    call: destructiveCall,
    mode: 'destructive',
    defaultExpanded: true,
  },
};

export const LocalAwaitingApproval: Story = {
  args: {
    call: localCall,
    mode: 'local',
    onApprove: () => {},
    onReject: () => {},
  },
};

export const CustomRenderers: Story = {
  args: {
    call: readCall,
    result: successResult,
    mode: 'read',
    defaultExpanded: true,
    renderArgs: (call) => (
      <div className="text-xs">
        <span className="font-medium text-agent-foreground">Query:</span>{' '}
        <span className="text-agent-muted-foreground">
          {String((call.arguments as { query?: string }).query ?? '')}
        </span>
      </div>
    ),
    renderResult: (result) => (
      <ul className="space-y-1 text-xs text-agent-muted-foreground">
        {((result.data as { hits?: Array<{ title: string; url: string }> } | undefined)?.hits ?? []).map(
          (hit) => (
            <li key={hit.url}>
              <a className="text-agent-accent underline" href={hit.url}>
                {hit.title}
              </a>
            </li>
          ),
        )}
      </ul>
    ),
  },
};
