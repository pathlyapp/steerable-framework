/**
 * 会话内提交的附件链路契约（回归守卫）。
 *
 * 背景：附件链路从上层仓移植进 shell 时，`AgentPage` 调用 `<LocalChatPanel>`
 * 漏传了 `chatId`，导致 `LocalChatPanel` 里那段 `saveChatAttachments` 永远
 * 走 else 分支——浏览器模式下 File 没有路径，写进正文的就是空引用，模型
 * 「收到了文件却读不到」。这里从组件层钉死：
 *   1. 传了 chatId → 提交时一定调用 attachments.save，并把落盘路径写进正文；
 *   2. 非图片文件（docx/pdf）同样落盘 + 写路径引用，不进 metadata.images；
 *   3. 图片才额外进 metadata.images（多模态是附加通道，不是文件通道）；
 *   4. 落盘失败不会产生 `- ``` `` 这种空引用，而是显示给用户。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/electron-bridge', () => ({
  isElectron: vi.fn(() => true),
  getElectronBridge: vi.fn(),
  runLocalBackend: vi.fn(),
}));

import { getElectronBridge } from '@/lib/electron-bridge';
import { LocalChatPanel } from './LocalChatPanel';

const saveMock = vi.fn();

beforeEach(() => {
  saveMock.mockReset();
  vi.mocked(getElectronBridge).mockReturnValue({
    attachments: { save: saveMock },
    // ChatInput 挂载时会拉 skills / MCP 列表；给个空实现避免测试噪音。
    localBackend: { request: vi.fn().mockResolvedValue({ skills: [], mcpTools: [] }) },
  } as never);
});

afterEach(() => {
  cleanup();
});

function pickFile(name: string, type = 'application/octet-stream') {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['hello'], name, { type });
  fireEvent.change(input, { target: { files: [file] } });
}

function renderPanel(onSubmit = vi.fn().mockResolvedValue(undefined), chatId: string | null = 'chat-1') {
  render(
    <LocalChatPanel
      chatId={chatId}
      messages={[]}
      agents={[]}
      currentAgent={null}
      onSubmit={onSubmit}
    />,
  );
  return onSubmit;
}

describe('LocalChatPanel 附件提交', () => {
  it('文档附件：落盘后把落盘路径写进正文，且不进 metadata.images', async () => {
    saveMock.mockResolvedValue({
      files: [{ name: 'a.docx', path: '/data/attachments/chat-1/a.docx', size: 5 }],
    });
    const onSubmit = renderPanel();

    pickFile('a.docx');
    await waitFor(() => expect(screen.getByText('a.docx')).toBeTruthy());
    fireEvent.click(screen.getByTestId('chat-send'));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledTimes(1);
    expect(saveMock.mock.calls[0][0].chatId).toBe('chat-1');
    const input = onSubmit.mock.calls[0][0] as { content: string; metadata?: Record<string, unknown> };
    expect(input.content).toContain('- `/data/attachments/chat-1/a.docx`');
    // 非图片文件不占用图片通道。
    expect(input.metadata?.images).toBeUndefined();
    // 绝不允许空路径引用。
    expect(input.content).not.toContain('- ``');
  });

  it('图片附件：落盘 + 正文路径 + 额外进 metadata.images', async () => {
    saveMock.mockResolvedValue({
      files: [{ name: 'p.png', path: '/data/attachments/chat-1/p.png', size: 5 }],
    });
    const onSubmit = renderPanel();

    pickFile('p.png', 'image/png');
    await waitFor(() => expect(screen.getByText('p.png')).toBeTruthy());
    fireEvent.click(screen.getByTestId('chat-send'));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const input = onSubmit.mock.calls[0][0] as { content: string; metadata?: { images?: unknown[] } };
    expect(input.content).toContain('- `/data/attachments/chat-1/p.png`');
    expect(input.metadata?.images).toEqual([
      { path: '/data/attachments/chat-1/p.png', name: 'p.png' },
    ]);
  });

  it('落盘失败：不写空引用、显示错误、无正文时不发送', async () => {
    saveMock.mockResolvedValue({
      files: [{ name: 'big.bin', path: '', size: 0, error: 'file too large' }],
    });
    const onSubmit = renderPanel();

    pickFile('big.bin');
    await waitFor(() => expect(screen.getByText('big.bin')).toBeTruthy());
    fireEvent.click(screen.getByTestId('chat-send'));

    await waitFor(() => expect(screen.getByTestId('attachment-error')).toBeTruthy());
    expect(screen.getByTestId('attachment-error').textContent).toContain('big.bin');
    // 没有任何可发送的内容 → 不提交，保留输入框让用户重试。
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('未传 chatId 时不落盘（契约：调用方必须传 chatId）', async () => {
    const onSubmit = renderPanel(vi.fn().mockResolvedValue(undefined), null);

    pickFile('a.docx');
    await waitFor(() => expect(screen.getByText('a.docx')).toBeTruthy());
    fireEvent.click(screen.getByTestId('chat-send'));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(saveMock).not.toHaveBeenCalled();
  });
});
