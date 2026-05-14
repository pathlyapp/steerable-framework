import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { SSEEvent } from '@steerable/agent-protocol';
import { SSEStreamView } from './SSEStreamView';

const events: SSEEvent[] = [
  { type: 'content', content: 'Hello world' },
  {
    type: 'tool_call',
    payload: { id: 'c1', name: 'get_weather', arguments: { city: 'SF' } },
  },
  { type: 'tool_result', payload: { success: true } },
  { type: 'done' },
];

describe('SSEStreamView', () => {
  it('renders one row per event with the type label uppercase', () => {
    render(<SSEStreamView events={events} />);
    expect(screen.getByText('content')).toBeTruthy();
    expect(screen.getByText('tool_call')).toBeTruthy();
    expect(screen.getByText('tool_result')).toBeTruthy();
    expect(screen.getByText('done')).toBeTruthy();
  });

  it('filters by type when filterTypes is provided', () => {
    render(<SSEStreamView events={events} filterTypes={['tool_call', 'tool_result']} />);
    expect(screen.queryByText('content')).toBeNull();
    expect(screen.queryByText('done')).toBeNull();
    expect(screen.getByText('tool_call')).toBeTruthy();
    expect(screen.getByText('tool_result')).toBeTruthy();
  });

  it('renders the empty state when there are no events', () => {
    render(<SSEStreamView events={[]} emptyState={<span>(idle)</span>} />);
    expect(screen.getByText('(idle)')).toBeTruthy();
  });

  it('renders pretty-printed JSON in verbose mode', () => {
    render(<SSEStreamView events={events.slice(1, 2)} verbose />);
    expect(screen.getByText(/"name": "get_weather"/)).toBeTruthy();
  });
});
