import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const request = vi.fn();

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: () => true,
  getElectronBridge: () => ({
    localBackend: { request },
    local: { saveTextFile: vi.fn() },
  }),
}));

const { InsightsConsentBanner, InsightsSettingsPanel } = await import('./InsightsSettingsPanel');

beforeEach(() => {
  request.mockReset();
  request.mockResolvedValue({
    installId: '550e8400-e29b-41d4-a716-446655440000',
    shareBehavior: false,
    shareConversation: false,
    shareProfile: false,
    profile: { displayName: '', email: '', company: '', note: '' },
    stats: { events: 2, turns: 1, profile: 0, pending: 3 },
  });
});

afterEach(cleanup);

describe('InsightsConsentBanner', () => {
  it('renders a simple upload-or-cancel first-run banner', async () => {
    render(<InsightsConsentBanner />);
    const banner = await screen.findByTestId('insights-consent-banner');
    expect(banner.className).toContain('bg-agent-muted');
    // 3.1 起品牌由产品注入；测试环境无注入 = shell 中性默认。
    expect(screen.getByText('帮助改进Steerable Shell')).toBeTruthy();
    expect(screen.getByText(/同意上传数据到服务器/)).toBeTruthy();
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.getByRole('button', { name: '保存' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '取消' })).toBeTruthy();
  });

  it('save agrees to upload; cancel keeps data local', async () => {
    render(<InsightsConsentBanner />);
    await screen.findByTestId('insights-consent-banner');
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'POST',
          path: '/api/v2/local-settings/insights',
          body: expect.objectContaining({
            shareBehavior: true,
            shareConversation: true,
            shareProfile: true,
            markPrompted: true,
          }),
        }),
      );
    });
  });

  it('cancel declines upload and still dismisses the prompt', async () => {
    render(<InsightsConsentBanner />);
    await screen.findByTestId('insights-consent-banner');
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'POST',
          path: '/api/v2/local-settings/insights',
          body: expect.objectContaining({
            shareBehavior: false,
            shareConversation: false,
            shareProfile: false,
            markPrompted: true,
          }),
        }),
      );
    });
  });

  it('does not open after the prompt has been answered', async () => {
    request.mockResolvedValueOnce({
      installId: '550e8400-e29b-41d4-a716-446655440000',
      shareBehavior: false,
      shareConversation: false,
      shareProfile: false,
      promptedAt: '2026-09-12T00:00:00.000Z',
      profile: { displayName: '', email: '', company: '', note: '' },
    });
    render(<InsightsConsentBanner />);
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        expect.objectContaining({ method: 'GET', path: '/api/v2/local-settings/insights' }),
      );
    });
    expect(screen.queryByTestId('insights-consent-banner')).toBeNull();
  });
});

describe('InsightsSettingsPanel', () => {
  it('shows separate toggles for behavior, conversation, and profile', async () => {
    render(<InsightsSettingsPanel />);
    expect(await screen.findByText(/帮助改进产品（行为 \/ 对话 \/ 用户信息 分开确认）/)).toBeTruthy();
    expect(screen.getAllByText(/自动上传/).length).toBe(3);
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
    expect(screen.getByText(/导出文件发给开发者/)).toBeTruthy();
    expect(screen.getByText(/现在上传本地记录/)).toBeTruthy();
  });

  it('saves the three flags independently', async () => {
    render(<InsightsSettingsPanel />);
    await screen.findByText(/本机已记/);
    const boxes = screen.getAllByRole('checkbox');
    fireEvent.click(boxes[0]);
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        expect.objectContaining({
          method: 'POST',
          path: '/api/v2/local-settings/insights',
          body: expect.objectContaining({
            shareBehavior: true,
            shareConversation: false,
            shareProfile: false,
          }),
        }),
      );
    });
  });
});
