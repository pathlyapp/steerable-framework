import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  OrchestrationPlanCard,
  type OrchestrationStep,
} from './OrchestrationPlanCard';

const steps: OrchestrationStep[] = [
  { id: '1', title: 'Plan trip', status: 'done', kind: 'agent:planner' },
  { id: '2', title: 'Book flights', status: 'running', kind: 'tool' },
  { id: '3', title: 'Reserve hotel', status: 'pending' },
  { id: '4', title: 'Email itinerary', status: 'error', description: 'SMTP timeout' },
];

describe('OrchestrationPlanCard', () => {
  it('renders the right summary count + each step with status data attribute', () => {
    render(<OrchestrationPlanCard steps={steps} />);
    expect(screen.getByText('1/4')).toBeTruthy();
    expect(screen.getByText('1 error')).toBeTruthy();
    expect(screen.getByText('Plan trip')).toBeTruthy();
    expect(screen.getByText('SMTP timeout')).toBeTruthy();
    const errorRow = screen.getByText('Email itinerary').closest('[data-status]');
    expect(errorRow?.getAttribute('data-status')).toBe('error');
  });

  it('makes rows clickable when onStepClick is provided and reports the right step', () => {
    const onStepClick = vi.fn();
    render(<OrchestrationPlanCard steps={steps} onStepClick={onStepClick} />);
    fireEvent.click(screen.getByText('Book flights'));
    expect(onStepClick).toHaveBeenCalledWith(
      expect.objectContaining({ id: '2', status: 'running' }),
    );
  });
});
