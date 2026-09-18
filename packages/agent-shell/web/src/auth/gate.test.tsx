import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../AppShell', () => ({
  AppShell: () => <div data-testid="application-router">application</div>,
}));
vi.mock('../layouts/AgentLayout', () => ({ AgentLayout: () => null }));
vi.mock('../pages/AgentPage', () => ({ AgentPage: () => null }));
vi.mock('../pages/SettingsPage', () => ({ SettingsPage: () => null }));
vi.mock('../packs/registry', () => ({ getPackRoutes: () => [] }));

afterEach(() => {
  cleanup();
  document.body.innerHTML = '';
  vi.resetModules();
});

async function loadModules() {
  const gate = await import('./gate');
  const main = await import('../main');
  return { ...gate, ...main };
}

function installRoot(): void {
  document.body.innerHTML = '<div id="root"></div>';
}

describe('app-shell gate', () => {
  it('bootstraps the application directly when no gate is registered', async () => {
    installRoot();
    const { bootstrap } = await loadModules();
    await bootstrap();
    expect(await screen.findByTestId('application-router')).toBeTruthy();
  });

  it('bootstraps the application directly when the gate is disabled', async () => {
    installRoot();
    const { bootstrap, registerAppShellGate } = await loadModules();
    registerAppShellGate({
      enabled: () => false,
      Component: () => <div>disabled gate</div>,
    });
    await bootstrap();
    expect(await screen.findByTestId('application-router')).toBeTruthy();
    expect(screen.queryByText('disabled gate')).toBeNull();
  });

  it('renders an enabled gate before creating the router and resumes once', async () => {
    installRoot();
    const { bootstrap, registerAppShellGate } = await loadModules();
    registerAppShellGate({
      enabled: () => true,
      Component: ({ onAuthenticated }) => (
        <button type="button" onClick={() => {
          onAuthenticated();
          onAuthenticated();
        }}>
          authenticate
        </button>
      ),
    });

    await bootstrap();
    expect(await screen.findByText('authenticate')).toBeTruthy();
    expect(screen.queryByTestId('application-router')).toBeNull();

    fireEvent.click(screen.getByText('authenticate'));
    await waitFor(() => {
      expect(screen.getByTestId('application-router')).toBeTruthy();
    });
    expect(screen.queryByText('authenticate')).toBeNull();
  });

  it('rejects duplicate gate registration', async () => {
    const { registerAppShellGate } = await loadModules();
    const gate = { enabled: () => false, Component: () => null };
    registerAppShellGate(gate);
    expect(() => registerAppShellGate(gate)).toThrow(
      '[app-shell-gate] a gate is already registered',
    );
  });
});
