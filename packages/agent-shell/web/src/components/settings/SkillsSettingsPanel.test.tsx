import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const request = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => true,
  getElectronBridge: () => ({
    localBackend: { request },
    local: {},
  }),
}));

const { SkillsSettingsPanel } = await import('./SkillsSettingsPanel');

beforeEach(() => {
  request.mockReset();
});

afterEach(cleanup);

describe('SkillsSettingsPanel', () => {
  it('shows a 工作区 badge and hides uninstall for workspace skills', async () => {
    request.mockResolvedValue({
      skills: [
        {
          name: 'compute-recursion',
          displayName: '递归函数计算',
          description: '算递推第 n 项',
          origin: 'workspace',
          isBuiltin: false,
          layer: 'catalog',
          modelInvocable: false,
        },
      ],
    });
    render(<SkillsSettingsPanel />);
    const row = await screen.findByTestId('skill-row-compute-recursion');
    expect(row.textContent).toContain('工作区');
    expect(row.textContent).not.toContain('内置');
    expect(screen.queryByTestId('skill-uninstall-compute-recursion')).toBeNull();
  });

  it('keeps uninstall on imported user skills', async () => {
    request.mockResolvedValue({
      skills: [
        {
          name: 'frontend-design',
          displayName: '',
          description: 'UI skill',
          origin: 'user',
          isBuiltin: false,
          layer: 'catalog',
          modelInvocable: true,
        },
      ],
    });
    render(<SkillsSettingsPanel />);
    await screen.findByTestId('skill-row-frontend-design');
    expect(screen.getByTestId('skill-uninstall-frontend-design')).toBeTruthy();
  });
});
