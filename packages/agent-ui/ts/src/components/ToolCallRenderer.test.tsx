/**
 * Render tests for `<ToolCallRenderer />`. We don't snapshot the whole DOM —
 * Tailwind class strings shift any time we tweak tokens — but we assert the
 * data-attributes (used by consumer styling overrides) and the visibility
 * rules for the approval row + result subtree.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ToolCallRenderer } from './ToolCallRenderer';

describe('ToolCallRenderer', () => {
  it('renders pending state with collapsed args by default', () => {
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'get_weather', arguments: { city: 'SF' } }}
      />,
    );
    const card = screen.getByText('get_weather').closest('[data-status]');
    expect(card?.getAttribute('data-status')).toBe('pending');
    expect(card?.getAttribute('data-mode')).toBe('read');
    // Args body is hidden until expanded.
    expect(screen.queryByText(/"city"/)).toBeNull();
  });

  it('expands args + result on click', () => {
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'create_event', arguments: { title: 'meeting' } }}
        result={{ success: true, data: { id: 'e1' } }}
      />,
    );
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText(/"title": "meeting"/)).toBeTruthy();
    expect(screen.getByText(/"id": "e1"/)).toBeTruthy();
  });

  it('renders done status when result.success=true', () => {
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'create_event', arguments: {} }}
        result={{ success: true }}
      />,
    );
    const card = screen.getByText('create_event').closest('[data-status]');
    expect(card?.getAttribute('data-status')).toBe('done');
  });

  it('renders error status and the error text in the result subtree when expanded', () => {
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'create_event', arguments: {} }}
        result={{ success: false, error: 'missing field' }}
      />,
    );
    const card = screen.getByText('create_event').closest('[data-status]');
    expect(card?.getAttribute('data-status')).toBe('error');
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('missing field')).toBeTruthy();
  });

  it('shows approve / reject buttons for local tools when handlers are provided', () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'local_run_script', arguments: { cmd: 'ls' } }}
        onApprove={onApprove}
        onReject={onReject}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    expect(onApprove).toHaveBeenCalledWith({
      id: 'c1',
      name: 'local_run_script',
      arguments: { cmd: 'ls' },
    });
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it('does not show approval row once a result has arrived', () => {
    const onApprove = vi.fn();
    render(
      <ToolCallRenderer
        call={{ id: 'c1', name: 'local_run_script', arguments: { cmd: 'ls' } }}
        result={{ success: true }}
        onApprove={onApprove}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();
  });
});
