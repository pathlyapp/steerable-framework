import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { SHOW_THINKING_CONTENT_STORAGE_KEY } from '@/lib/show-thinking-content';
import { AppearanceSettingsPanel } from './AppearanceSettingsPanel';

afterEach(() => {
  cleanup();
  localStorage.removeItem(SHOW_THINKING_CONTENT_STORAGE_KEY);
});

describe('AppearanceSettingsPanel', () => {
  it('defaults to 显示5行', () => {
    render(<AppearanceSettingsPanel />);
    expect(screen.getByTestId('thinking-display-peek').getAttribute('aria-checked')).toBe('true');
    expect(screen.getByTestId('thinking-display-hidden').getAttribute('aria-checked')).toBe(
      'false',
    );
    expect(screen.getByTestId('thinking-display-full').getAttribute('aria-checked')).toBe('false');
  });

  it('persists the segmented choice immediately', () => {
    render(<AppearanceSettingsPanel />);
    fireEvent.click(screen.getByTestId('thinking-display-hidden'));
    expect(screen.getByTestId('thinking-display-hidden').getAttribute('aria-checked')).toBe('true');
    expect(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('hidden');
    fireEvent.click(screen.getByTestId('thinking-display-full'));
    expect(screen.getByTestId('thinking-display-full').getAttribute('aria-checked')).toBe('true');
    expect(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('full');
    fireEvent.click(screen.getByTestId('thinking-display-peek'));
    expect(screen.getByTestId('thinking-display-peek').getAttribute('aria-checked')).toBe('true');
    expect(localStorage.getItem(SHOW_THINKING_CONTENT_STORAGE_KEY)).toBe('peek');
  });
});
