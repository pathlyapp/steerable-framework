import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SHOW_THINKING_CONTENT_STORAGE_KEY } from '@/lib/show-thinking-content';
import { AppearanceSettingsPanel } from './AppearanceSettingsPanel';

afterEach(() => {
  cleanup();
  localStorage.removeItem(SHOW_THINKING_CONTENT_STORAGE_KEY);
});

describe('AppearanceSettingsPanel', () => {
  it('defaults the thinking-content switch off', () => {
    render(<AppearanceSettingsPanel />);
    const toggle = screen.getByTestId('show-thinking-content-toggle');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
  });

  it('persists the switch immediately', () => {
    render(<AppearanceSettingsPanel />);
    fireEvent.click(screen.getByTestId('show-thinking-content-toggle'));
    expect(screen.getByTestId('show-thinking-content-toggle').getAttribute('aria-checked')).toBe(
      'true',
    );
    expect(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('1');
    fireEvent.click(screen.getByTestId('show-thinking-content-toggle'));
    expect(screen.getByTestId('show-thinking-content-toggle').getAttribute('aria-checked')).toBe(
      'false',
    );
    expect(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('0');
  });
});
