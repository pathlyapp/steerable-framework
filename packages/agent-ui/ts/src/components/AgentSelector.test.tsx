import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ChatAgent } from '@steerable/agent-protocol';
import { AgentSelector } from './AgentSelector';

const NOW = '2026-05-16T00:00:00Z';
const AGENTS: ChatAgent[] = [
  { id: 'a1', name: 'Alpha', createdAt: NOW, updatedAt: NOW },
  { id: 'a2', name: 'Beta', createdAt: NOW, updatedAt: NOW },
  { id: 'a3', name: 'Gamma', createdAt: NOW, updatedAt: NOW },
];

describe('AgentSelector', () => {
  it('renders all agents and marks the selected radio', () => {
    render(
      <AgentSelector agents={AGENTS} selectedId="a2" onSelect={() => {}} />,
    );

    expect(
      screen.getByRole('radio', { name: 'Alpha' }).getAttribute('aria-checked'),
    ).toBe('false');
    expect(
      screen.getByRole('radio', { name: 'Beta' }).getAttribute('aria-checked'),
    ).toBe('true');
    expect(
      screen.getByRole('radio', { name: 'Gamma' }).getAttribute('aria-checked'),
    ).toBe('false');
  });

  it('calls onSelect on click', () => {
    const onSelect = vi.fn();
    render(<AgentSelector agents={AGENTS} selectedId="a1" onSelect={onSelect} />);

    fireEvent.click(screen.getByRole('radio', { name: 'Gamma' }));
    expect(onSelect).toHaveBeenCalledWith('a3');
  });

  it('supports keyboard navigation (Arrow, Home, End)', () => {
    const onSelect = vi.fn();
    render(<AgentSelector agents={AGENTS} selectedId="a2" onSelect={onSelect} />);

    const beta = screen.getByRole('radio', { name: 'Beta' });
    fireEvent.keyDown(beta, { key: 'ArrowRight' });
    fireEvent.keyDown(beta, { key: 'ArrowLeft' });
    fireEvent.keyDown(beta, { key: 'Home' });
    fireEvent.keyDown(beta, { key: 'End' });

    expect(onSelect).toHaveBeenNthCalledWith(1, 'a3');
    expect(onSelect).toHaveBeenNthCalledWith(2, 'a1');
    expect(onSelect).toHaveBeenNthCalledWith(3, 'a1');
    expect(onSelect).toHaveBeenNthCalledWith(4, 'a3');
  });

  it('does not call onSelect when disabled', () => {
    const onSelect = vi.fn();
    render(
      <AgentSelector
        agents={AGENTS}
        selectedId="a1"
        onSelect={onSelect}
        disabled
      />,
    );

    const alpha = screen.getByRole('radio', { name: 'Alpha' });
    fireEvent.click(alpha);
    fireEvent.keyDown(alpha, { key: 'ArrowRight' });
    expect(onSelect).not.toHaveBeenCalled();
  });
});
