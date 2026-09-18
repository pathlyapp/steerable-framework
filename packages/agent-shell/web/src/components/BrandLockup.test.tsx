import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const brandTitle = vi.hoisted(() => ({ value: '侧栏标题' }));

vi.mock('@/brand', () => ({
  BRAND_NAME: '窗口标题名',
  get BRAND_TITLE() {
    return brandTitle.value;
  },
  getBrandLogoUrl: () => 'logo://mark',
}));

import { BrandLockup } from './BrandLockup';

afterEach(() => {
  cleanup();
  brandTitle.value = '侧栏标题';
});

describe('BrandLockup', () => {
  it('配了标题时并排显示 logo 与标题，logo 不锁成方图', () => {
    render(<BrandLockup />);
    const logo = screen.getByRole('img', { name: '侧栏标题' });
    expect(logo.getAttribute('src')).toBe('logo://mark');
    expect(logo.className).toContain('w-auto');
    expect(logo.className).not.toMatch(/(?:^|\s)w-6(?:\s|$)/);
    expect(screen.getByText('侧栏标题')).toBeTruthy();
    expect(screen.queryByText('窗口标题名')).toBeNull();
  });

  it('未配标题时只显示等比例 logo', () => {
    brandTitle.value = '';
    render(<BrandLockup />);
    const logo = screen.getByRole('img', { name: '窗口标题名' });
    expect(logo.className).toContain('w-auto');
    expect(screen.queryByText('窗口标题名')).toBeNull();
  });
});
