import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SuggestedReplies } from './SuggestedReplies';
import { extractLatestSuggestedReplies } from './suggested-replies-model';

afterEach(cleanup);

describe('SuggestedReplies', () => {
  it('点击芯片把原文交给 onSelect', () => {
    const onSelect = vi.fn();
    render(
      <SuggestedReplies
        suggestions={['调整封面配色', '把个人简介写得更具体', '再加一页项目案例']}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText('把个人简介写得更具体'));
    expect(onSelect).toHaveBeenCalledWith('把个人简介写得更具体');
    const chips = screen.getByTestId('suggested-replies').querySelectorAll('[data-testid="suggested-reply"]');
    expect(chips).toHaveLength(3);
    expect(chips[0].className).toContain('text-[12px]');
  });

  it('技能下一步多于 3 条时全部画出', () => {
    render(
      <SuggestedReplies
        suggestions={[
          '画接底层井密度交会图',
          '再画一层井密度交会图',
          '统计本区有效厚度',
          '输出气层厚度分层表',
          '绘制气层厚度等值线',
        ]}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByTestId('suggested-replies').querySelectorAll('[data-testid="suggested-reply"]')).toHaveLength(5);
  });

  it('空列表不渲染', () => {
    const { container } = render(<SuggestedReplies suggestions={[]} onSelect={() => {}} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('extractLatestSuggestedReplies', () => {
  it('取时间上最后一条助手消息里的 suggestedReplies', () => {
    expect(
      extractLatestSuggestedReplies([
        {
          id: 'u1',
          role: 'user',
          content: '做 ppt',
          createdAt: '2026-09-17T08:00:00.000Z',
        },
        {
          id: 'a1',
          role: 'assistant',
          content: '旧回复',
          createdAt: '2026-09-17T08:01:00.000Z',
          messageMetadata: JSON.stringify({ suggestedReplies: ['旧1', '旧2', '旧3'] }),
        },
        {
          id: 'a2',
          role: 'assistant',
          content: '新回复',
          createdAt: '2026-09-17T08:02:00.000Z',
          messageMetadata: JSON.stringify({
            suggestedReplies: ['调整封面配色', '把个人简介写得更具体', '再加一页项目案例'],
          }),
        },
      ]),
    ).toEqual({
      messageId: 'a2',
      suggestions: ['调整封面配色', '把个人简介写得更具体', '再加一页项目案例'],
    });
  });

  it('超过 3 条时不再截成 3 条', () => {
    expect(
      extractLatestSuggestedReplies([
        {
          id: 'a1',
          role: 'assistant',
          content: '新回复',
          createdAt: '2026-09-17T08:02:00.000Z',
          messageMetadata: JSON.stringify({
            suggestedReplies: [
              '画接底层井密度交会图',
              '再画一层井密度交会图',
              '统计本区有效厚度',
              '输出气层厚度分层表',
              '绘制气层厚度等值线',
            ],
          }),
        },
      ]),
    ).toEqual({
      messageId: 'a1',
      suggestions: [
        '画接底层井密度交会图',
        '再画一层井密度交会图',
        '统计本区有效厚度',
        '输出气层厚度分层表',
        '绘制气层厚度等值线',
      ],
    });
  });

  it('没有建议时返回 null', () => {
    expect(
      extractLatestSuggestedReplies([
        { id: 'a1', role: 'assistant', content: 'hi', createdAt: '2026-09-17T08:00:00.000Z' },
      ]),
    ).toBeNull();
  });
});
