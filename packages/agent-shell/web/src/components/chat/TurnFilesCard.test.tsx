/**
 * TurnFilesCard 契约：回合产物逐行可点（点击 = 系统默认应用打开），
 * 打开失败在行内给出原因；空列表不渲染。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { openLocalPath } from '@/lib/local-api';
import { TurnFilesCard } from './TurnFilesCard';
import type { TurnFile } from './turn-files';

vi.mock('@/lib/local-api', () => ({
  openLocalPath: vi.fn(async () => ({ success: true })),
}));

const openLocalPathMock = vi.mocked(openLocalPath);

afterEach(() => {
  cleanup();
  openLocalPathMock.mockClear();
  openLocalPathMock.mockResolvedValue({ success: true });
});

function makeFile(overrides: Partial<TurnFile> = {}): TurnFile {
  return {
    path: '/proj/自我介绍.pptx',
    kind: 'created',
    size: 2048,
    ...overrides,
  };
}

describe('TurnFilesCard', () => {
  it('空列表不渲染', () => {
    const { container } = render(<TurnFilesCard files={[]} />);
    expect(container.querySelector('[data-turn-files]')).toBeNull();
  });

  it('列出产物文件：标题计数 + 文件名 / 目录 / 大小', () => {
    render(
      <TurnFilesCard
        files={[
          makeFile(),
          makeFile({ path: '/proj/README.md', kind: 'modified', size: 512 }),
        ]}
      />,
    );

    expect(screen.getByText('本轮产生了 2 个文件，点击打开')).toBeTruthy();
    expect(screen.getByText('自我介绍.pptx')).toBeTruthy();
    expect(screen.getAllByText('/proj/')).toHaveLength(2);
    expect(screen.getByText('2.0 KB')).toBeTruthy();
    expect(screen.getByText('README.md')).toBeTruthy();
    expect(screen.getByText('512 B')).toBeTruthy();
    expect(
      document.querySelector('[data-turn-file]')?.getAttribute('data-kind'),
    ).toBe('created');
  });

  it('点击行用系统默认应用打开该文件', async () => {
    render(<TurnFilesCard files={[makeFile()]} />);

    fireEvent.click(screen.getByText('自我介绍.pptx'));
    expect(openLocalPathMock).toHaveBeenCalledWith('/proj/自我介绍.pptx');

    // 成功后不出现行内错误。
    await screen.findByText('自我介绍.pptx');
    expect(screen.queryByText(/打开失败/)).toBeNull();
  });

  it('打开失败时在行内显示原因', async () => {
    openLocalPathMock.mockResolvedValue({ success: false, error: '没有应用能打开该文件' });
    render(<TurnFilesCard files={[makeFile()]} />);

    fireEvent.click(screen.getByText('自我介绍.pptx'));
    await screen.findByText(/没有应用能打开该文件/);
  });

  it('bridge 缺失（纯浏览器 dev）抛错时同样落成行内错误', async () => {
    openLocalPathMock.mockRejectedValue(new Error('Electron bridge unavailable'));
    render(<TurnFilesCard files={[makeFile()]} />);

    fireEvent.click(screen.getByText('自我介绍.pptx'));
    await screen.findByText(/Electron bridge unavailable/);
  });
});
