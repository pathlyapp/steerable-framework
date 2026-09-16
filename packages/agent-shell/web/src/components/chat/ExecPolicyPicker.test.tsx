import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExecPolicyPicker } from './ExecPolicyPicker';

afterEach(() => cleanup());

describe('ExecPolicyPicker', () => {
  it('shows the current policy and switches to full access', () => {
    const onChange = vi.fn();
    render(
      <ExecPolicyPicker policy="workspace" disabled={false} onChange={onChange} />,
    );

    fireEvent.click(screen.getByTestId('exec-policy-picker'));
    fireEvent.click(screen.getByTestId('exec-policy-full'));
    expect(onChange).toHaveBeenCalledWith('full');
  });

  it('does not open when disabled', () => {
    render(
      <ExecPolicyPicker policy="workspace" disabled onChange={vi.fn()} />,
    );
    fireEvent.click(screen.getByTestId('exec-policy-picker'));
    expect(screen.queryByTestId('exec-policy-full')).toBeNull();
  });
});
