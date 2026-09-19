import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const resolveLocalPaths = vi.fn();
const openLocalPath = vi.fn();
vi.mock('@/lib/local-api', () => ({
  resolveLocalPaths: (...args: unknown[]) => resolveLocalPaths(...args),
  openLocalPath: (...args: unknown[]) => openLocalPath(...args),
}));

const { FilePathCode } = await import('./FilePathCode');
const { resetPathMentionCache } = await import('./path-mentions');

beforeEach(() => {
  resetPathMentionCache();
  resolveLocalPaths.mockReset();
  openLocalPath.mockReset();
  resolveLocalPaths.mockResolvedValue({ resolved: [] });
  openLocalPath.mockResolvedValue({ success: true });
});

afterEach(() => {
  cleanup();
  resetPathMentionCache();
});

describe('FilePathCode', () => {
  it('后端确认不存在时渲染成普通行内代码（不可点击）', async () => {
    render(
      <FilePathCode candidate="./missing.pptx" chatId="chat-1">
        ./missing.pptx
      </FilePathCode>,
    );
    await waitFor(() => expect(resolveLocalPaths).toHaveBeenCalled());
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('./missing.pptx')).toBeTruthy();
  });

  it('确认存在时变成按钮，点击用绝对路径调 openLocalPath', async () => {
    resolveLocalPaths.mockResolvedValue({
      resolved: [
        { candidate: './自我介绍.pptx', path: '/proj/自我介绍.pptx', isDirectory: false },
      ],
    });
    render(
      <FilePathCode candidate="./自我介绍.pptx" chatId="chat-1">
        ./自我介绍.pptx
      </FilePathCode>,
    );

    const button = await screen.findByRole('button');
    expect(button.textContent).toBe('自我介绍.pptx');
    expect(button.getAttribute('title')).toBe('点击打开 /proj/自我介绍.pptx');
    fireEvent.click(button);
    await waitFor(() => expect(openLocalPath).toHaveBeenCalledWith('/proj/自我介绍.pptx'));
  });

  it('打开失败时把错误挂在 title 上并标红', async () => {
    resolveLocalPaths.mockResolvedValue({
      resolved: [{ candidate: './a.pptx', path: '/proj/a.pptx', isDirectory: false }],
    });
    openLocalPath.mockResolvedValue({ success: false, error: 'ENOENT' });
    render(
      <FilePathCode candidate="./a.pptx" chatId="chat-1">
        ./a.pptx
      </FilePathCode>,
    );

    const button = await screen.findByRole('button');
    fireEvent.click(button);
    await waitFor(() =>
      expect(button.getAttribute('title')).toBe('/proj/a.pptx（ENOENT）'),
    );
    expect(button.className).toContain('text-red-600');
  });

  it('可点击时只显示文件名，完整路径放在 title', async () => {
    const full =
      'C:\\Users\\wangtai\\projects\\pathlyapp\\deeppath-agent\\output\\自我介绍_简约商务.pptx';
    resolveLocalPaths.mockResolvedValue({
      resolved: [{ candidate: full, path: full, isDirectory: false }],
    });
    render(
      <FilePathCode candidate={full} chatId="chat-1">
        {full}
      </FilePathCode>,
    );

    const button = await screen.findByRole('button');
    expect(button.textContent).toBe('自我介绍_简约商务.pptx');
    expect(button.textContent).not.toContain('C:\\Users');
    expect(button.getAttribute('title')).toBe(`点击打开 ${full}`);
  });
});
