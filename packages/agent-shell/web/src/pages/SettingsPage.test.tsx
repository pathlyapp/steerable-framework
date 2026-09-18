import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { save } = vi.hoisted(() => ({ save: vi.fn(async () => {}) }));

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => true,
}));

vi.mock('@/components/settings/LlmSettingsPanel', async () => {
  const react = await import('react');
  return {
    LlmSettingsPanel: react.forwardRef(function MockLlm(
      props: { onSaveUiChange?: (ui: { saving: boolean; savedOk: boolean; loading: boolean }) => void },
      ref,
    ) {
      react.useImperativeHandle(ref, () => ({ save }));
      react.useEffect(() => {
        props.onSaveUiChange?.({ saving: false, savedOk: false, loading: false });
      }, [props.onSaveUiChange]);
      return <div data-testid="llm-panel" />;
    }),
  };
});

vi.mock('@/components/settings/InsightsSettingsPanel', () => ({
  InsightsSettingsPanel: () => null,
}));
vi.mock('@/components/settings/TelemetrySettingsPanel', () => ({
  TelemetrySettingsPanel: () => null,
}));
vi.mock('@/components/settings/UsagePanel', () => ({
  UsagePanel: () => null,
}));
vi.mock('@/components/settings/WebSearchSettingsPanel', () => ({
  WebSearchSettingsPanel: () => null,
}));
vi.mock('@/components/settings/SecuritySettingsPanel', () => ({
  SecuritySettingsPanel: () => null,
}));
vi.mock('@/components/settings/SkillsSettingsPanel', () => ({
  SkillsSettingsPanel: () => null,
}));
vi.mock('@/components/settings/McpSettingsPanel', () => ({
  McpSettingsPanel: () => null,
}));
vi.mock('@/components/settings/AgentsSettingsPanel', () => ({
  AgentsSettingsPanel: () => null,
}));

const { SettingsPage } = await import('./SettingsPage');

function renderSettings(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <SettingsPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  save.mockClear();
});

describe('SettingsPage header save', () => {
  it('saves LLM settings from the general settings header', () => {
    renderSettings();
    fireEvent.click(screen.getByTestId('settings-header-save'));
    expect(save).toHaveBeenCalledTimes(1);
  });

  it('does not show the header save on skills, MCP, or agents pages', () => {
    const skills = renderSettings('?section=skills');
    expect(screen.queryByTestId('settings-header-save')).toBeNull();
    skills.unmount();
    const mcp = renderSettings('?section=mcp');
    expect(screen.queryByTestId('settings-header-save')).toBeNull();
    mcp.unmount();
    renderSettings('?section=agents');
    expect(screen.queryByTestId('settings-header-save')).toBeNull();
  });

  it('orders general sections by how often they are used', () => {
    renderSettings();
    expect(screen.getByRole('heading', { name: '界面' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '帮助改进产品' })).toBeTruthy();
    expect(
      [...document.querySelectorAll('[data-testid^="settings-section-"]')].map(
        (el) => el.getAttribute('data-testid'),
      ),
    ).toEqual([
      'settings-section-appearance',
      'settings-section-llm',
      'settings-section-web-search',
      'settings-section-usage',
      'settings-section-diagnose',
      'settings-section-security',
      'settings-section-insights',
      'settings-section-telemetry',
    ]);
  });
});
